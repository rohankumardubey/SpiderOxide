use std::collections::HashMap;
use std::io;
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use async_compression::tokio::bufread::{BrotliDecoder, GzipDecoder, ZlibDecoder, ZstdDecoder};
use futures_util::TryStreamExt;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use reqwest::Url;
use reqwest::cookie::{CookieStore, Jar};
use reqwest::header::{
    ACCEPT_ENCODING, CONTENT_ENCODING, COOKIE, HeaderMap, HeaderName, HeaderValue,
    PROXY_AUTHORIZATION,
};
use reqwest::{Client, ClientBuilder, Method, Proxy, Response, Version, redirect};
use tokio::io::{AsyncRead, AsyncReadExt, BufReader};
use tokio_util::io::StreamReader;

use crate::NativeDownloadError;

type ResponseReader = Pin<Box<dyn AsyncRead + Send>>;

fn download_error(message: impl Into<String>) -> PyErr {
    NativeDownloadError::new_err(message.into())
}

fn protocol_name(version: Version) -> String {
    if version == Version::HTTP_09 {
        "HTTP/0.9".to_owned()
    } else if version == Version::HTTP_10 {
        "HTTP/1.0".to_owned()
    } else if version == Version::HTTP_11 {
        "HTTP/1.1".to_owned()
    } else if version == Version::HTTP_2 {
        "HTTP/2".to_owned()
    } else if version == Version::HTTP_3 {
        "HTTP/3".to_owned()
    } else {
        format!("{version:?}")
    }
}

fn content_encodings(headers: &HeaderMap) -> Vec<String> {
    headers
        .get_all(CONTENT_ENCODING)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(|value| value.trim().to_ascii_lowercase())
        .filter(|value| !value.is_empty())
        .collect()
}

fn response_reader(response: Response, encodings: &[String]) -> ResponseReader {
    let stream = response.bytes_stream().map_err(io::Error::other);
    let mut reader: ResponseReader = Box::pin(StreamReader::new(stream));
    if !encodings.iter().all(|value| {
        matches!(
            value.as_str(),
            "br" | "deflate" | "gzip" | "identity" | "zstd"
        )
    }) {
        return reader;
    }

    for encoding in encodings.iter().rev() {
        reader = match encoding.as_str() {
            "br" => Box::pin(BrotliDecoder::new(BufReader::new(reader))),
            "deflate" => Box::pin(ZlibDecoder::new(BufReader::new(reader))),
            "gzip" => Box::pin(GzipDecoder::new(BufReader::new(reader))),
            "identity" => reader,
            "zstd" => Box::pin(ZstdDecoder::new(BufReader::new(reader))),
            _ => unreachable!("content encodings were validated before decoding"),
        };
    }
    reader
}

fn merge_stored_cookies(headers: &mut HeaderMap, cookie_jar: &Jar, url: &str) -> PyResult<()> {
    let explicit = headers
        .get_all(COOKIE)
        .iter()
        .map(HeaderValue::as_bytes)
        .collect::<Vec<_>>();
    if explicit.is_empty() {
        return Ok(());
    }

    let parsed_url = Url::parse(url)
        .map_err(|error| download_error(format!("invalid request URL {url:?}: {error}")))?;
    let Some(stored) = cookie_jar.cookies(&parsed_url) else {
        return Ok(());
    };
    let mut combined = explicit.join(b"; ".as_slice());
    if !combined.is_empty() && !stored.as_bytes().is_empty() {
        combined.extend_from_slice(b"; ");
    }
    combined.extend_from_slice(stored.as_bytes());
    let value = HeaderValue::from_bytes(&combined)
        .map_err(|error| download_error(format!("invalid Cookie header: {error}")))?;
    headers.remove(COOKIE);
    headers.insert(COOKIE, value);
    Ok(())
}

fn client_builder(cookie_jar: Arc<Jar>, user_agent: Option<&str>) -> ClientBuilder {
    let mut default_headers = HeaderMap::new();
    default_headers.insert(
        ACCEPT_ENCODING,
        HeaderValue::from_static("gzip, br, deflate, zstd"),
    );
    let mut builder = Client::builder()
        .cookie_provider(cookie_jar)
        .default_headers(default_headers)
        .redirect(redirect::Policy::none())
        .no_proxy();
    if let Some(user_agent) = user_agent.filter(|value| !value.is_empty()) {
        builder = builder.user_agent(user_agent);
    }
    builder
}

fn build_client(
    cookie_jar: Arc<Jar>,
    user_agent: Option<&str>,
    proxy: Option<(&str, Option<&[u8]>)>,
) -> PyResult<Client> {
    let mut builder = client_builder(cookie_jar, user_agent);
    if let Some((url, authorization)) = proxy {
        let mut configured = Proxy::all(url)
            .map_err(|error| download_error(format!("invalid proxy URL: {error}")))?;
        if let Some(authorization) = authorization {
            let header = HeaderValue::from_bytes(authorization).map_err(|error| {
                download_error(format!("invalid Proxy-Authorization header: {error}"))
            })?;
            configured = configured.custom_http_auth(header);
        }
        builder = builder.proxy(configured);
    }
    builder
        .build()
        .map_err(|error| PyValueError::new_err(format!("invalid downloader settings: {error}")))
}

#[derive(Clone, Eq, Hash, PartialEq)]
struct ProxyClientKey {
    url: String,
    authorization: Option<Vec<u8>>,
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeHttpResponse {
    url: String,
    status: u16,
    headers: Vec<(String, Vec<u8>)>,
    body: Vec<u8>,
    protocol: String,
    latency: f64,
}

#[pymethods]
impl NativeHttpResponse {
    #[getter]
    fn url(&self) -> &str {
        &self.url
    }

    #[getter]
    fn status(&self) -> u16 {
        self.status
    }

    #[getter]
    fn headers(&self, py: Python<'_>) -> Vec<(String, Py<PyBytes>)> {
        self.headers
            .iter()
            .map(|(name, value)| (name.clone(), PyBytes::new(py, value).unbind()))
            .collect()
    }

    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body)
    }

    #[getter]
    fn protocol(&self) -> &str {
        &self.protocol
    }

    #[getter]
    fn latency(&self) -> f64 {
        self.latency
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeHttpClient {
    client: Client,
    proxy_clients: Mutex<HashMap<ProxyClientKey, Client>>,
    cookie_jar: Arc<Jar>,
    user_agent: Option<String>,
    max_size: usize,
    timeout: Duration,
}

impl NativeHttpClient {
    fn lock_proxy_clients(&self) -> PyResult<MutexGuard<'_, HashMap<ProxyClientKey, Client>>> {
        self.proxy_clients
            .lock()
            .map_err(|_| PyRuntimeError::new_err("native proxy client pool was poisoned"))
    }

    fn client_for_proxy(
        &self,
        proxy: Option<&str>,
        authorization: Option<&[u8]>,
    ) -> PyResult<Client> {
        let Some(proxy) = proxy else {
            return Ok(self.client.clone());
        };
        let key = ProxyClientKey {
            url: proxy.to_owned(),
            authorization: authorization.map(<[u8]>::to_vec),
        };
        let mut clients = self.lock_proxy_clients()?;
        if let Some(client) = clients.get(&key) {
            return Ok(client.clone());
        }
        let client = build_client(
            self.cookie_jar.clone(),
            self.user_agent.as_deref(),
            Some((proxy, authorization)),
        )?;
        clients.insert(key, client.clone());
        Ok(client)
    }
}

#[pymethods]
impl NativeHttpClient {
    #[new]
    #[pyo3(signature = (timeout = 180.0, max_size = 0, user_agent = None))]
    fn new(timeout: f64, max_size: usize, user_agent: Option<&str>) -> PyResult<Self> {
        let timeout = Duration::try_from_secs_f64(timeout).map_err(|_| {
            PyValueError::new_err("DOWNLOAD_TIMEOUT must be a positive finite number")
        })?;
        if timeout.is_zero() {
            return Err(PyValueError::new_err(
                "DOWNLOAD_TIMEOUT must be a positive finite number",
            ));
        }

        let cookie_jar = Arc::new(Jar::default());
        let user_agent = user_agent.map(str::to_owned);
        let client = build_client(cookie_jar.clone(), user_agent.as_deref(), None)?;
        Ok(Self {
            client,
            proxy_clients: Mutex::new(HashMap::new()),
            cookie_jar,
            user_agent,
            max_size,
            timeout,
        })
    }

    #[pyo3(signature = (url, method, headers, body, proxy = None))]
    fn fetch<'py>(
        &self,
        py: Python<'py>,
        url: String,
        method: String,
        headers: Vec<(String, Vec<u8>)>,
        body: Vec<u8>,
        proxy: Option<(String, Option<Vec<u8>>)>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client_for_proxy(
            proxy.as_ref().map(|(url, _)| url.as_str()),
            proxy
                .as_ref()
                .and_then(|(_, authorization)| authorization.as_deref()),
        )?;
        let cookie_jar = self.cookie_jar.clone();
        let max_size = self.max_size;
        let timeout = self.timeout;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let parsed_method = Method::from_bytes(method.as_bytes()).map_err(|error| {
                download_error(format!("invalid HTTP method {method:?}: {error}"))
            })?;
            let mut parsed_headers = HeaderMap::new();
            for (name, value) in headers {
                let parsed_name = HeaderName::from_bytes(name.as_bytes()).map_err(|error| {
                    download_error(format!("invalid HTTP header name {name:?}: {error}"))
                })?;
                let parsed_value = HeaderValue::from_bytes(&value).map_err(|error| {
                    download_error(format!("invalid value for HTTP header {name:?}: {error}"))
                })?;
                parsed_headers.append(parsed_name, parsed_value);
            }
            parsed_headers.remove(PROXY_AUTHORIZATION);
            merge_stored_cookies(&mut parsed_headers, &cookie_jar, &url)?;

            let request = client
                .request(parsed_method, &url)
                .headers(parsed_headers)
                .body(body);
            let started = Instant::now();
            let response = tokio::time::timeout(timeout, request.send())
                .await
                .map_err(|_| {
                    download_error(format!(
                        "unable to download {url}: timed out after {} seconds",
                        timeout.as_secs_f64()
                    ))
                })?
                .map_err(|error| download_error(format!("unable to download {url}: {error}")))?;
            let latency = started.elapsed().as_secs_f64();

            if let Some(declared_size) = response.content_length()
                && max_size != 0
                && declared_size > max_size as u64
            {
                return Err(download_error(format!(
                    "response exceeded DOWNLOAD_MAXSIZE ({max_size} bytes)"
                )));
            }

            let final_url = response.url().to_string();
            let status = response.status().as_u16();
            let protocol = protocol_name(response.version());
            let encodings = content_encodings(response.headers());
            let response_headers = response
                .headers()
                .iter()
                .map(|(name, value)| (name.as_str().to_owned(), value.as_bytes().to_vec()))
                .collect();

            let mut reader = response_reader(response, &encodings);
            let mut response_body = Vec::new();
            let mut chunk = [0_u8; 16 * 1024];
            loop {
                let bytes_read = tokio::time::timeout(timeout, reader.read(&mut chunk))
                    .await
                    .map_err(|_| {
                        download_error(format!(
                            "unable to read response body from {final_url}: timed out after {} seconds",
                            timeout.as_secs_f64()
                        ))
                    })?
                    .map_err(|error| {
                        download_error(format!(
                            "unable to read response body from {final_url}: {error}"
                        ))
                    })?;
                if bytes_read == 0 {
                    break;
                }
                let next_size = response_body
                    .len()
                    .checked_add(bytes_read)
                    .ok_or_else(|| download_error("response body size overflowed"))?;
                if max_size != 0 && next_size > max_size {
                    return Err(download_error(format!(
                        "response exceeded DOWNLOAD_MAXSIZE ({max_size} bytes)"
                    )));
                }
                response_body.extend_from_slice(&chunk[..bytes_read]);
            }

            Python::with_gil(|py| {
                Py::new(
                    py,
                    NativeHttpResponse {
                        url: final_url,
                        status,
                        headers: response_headers,
                        body: response_body,
                        protocol,
                        latency,
                    },
                )
            })
        })
    }

    #[getter]
    fn proxy_client_count(&self) -> PyResult<usize> {
        Ok(self.lock_proxy_clients()?.len())
    }
}
