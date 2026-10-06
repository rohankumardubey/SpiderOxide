use std::collections::{HashMap, VecDeque};
use std::error::Error;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use async_compression::tokio::bufread::{BrotliDecoder, GzipDecoder, ZlibDecoder, ZstdDecoder};
use futures_util::TryStreamExt;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use reqwest::dns::{Addrs, Name, Resolve, Resolving};
use reqwest::header::{
    ACCEPT_ENCODING, CONTENT_ENCODING, HeaderMap, HeaderName, HeaderValue, PROXY_AUTHORIZATION,
};
use reqwest::tls::{TlsInfo, Version as TlsVersion};
use reqwest::{Client, ClientBuilder, Identity, Method, Proxy, Response, Version, redirect};
use tokio::io::{AsyncRead, AsyncReadExt, BufReader};
use tokio_util::io::StreamReader;

use crate::{
    NativeCannotResolveHostError, NativeConnectionRefusedError, NativeDownloadCancelledError,
    NativeDownloadError, NativeDownloadTimeoutError, NativeResponseDataLossError,
    NativeUnsupportedSchemeError,
};

type ResponseReader = Pin<Box<dyn AsyncRead + Send>>;
type ProxyCredentials<'a> = (&'a str, &'a str);
type ProxyConfig<'a> = (&'a str, Option<&'a [u8]>, Option<ProxyCredentials<'a>>);
type OwnedProxyConfig = (String, Option<Vec<u8>>, Option<(String, String)>);

fn download_error(message: impl Into<String>) -> PyErr {
    NativeDownloadError::new_err(message.into())
}

fn timeout_error(message: impl Into<String>) -> PyErr {
    NativeDownloadTimeoutError::new_err(message.into())
}

fn data_loss_error(message: impl Into<String>) -> PyErr {
    NativeResponseDataLossError::new_err(message.into())
}

fn cancelled_error(message: impl Into<String>) -> PyErr {
    NativeDownloadCancelledError::new_err(message.into())
}

fn request_error(url: &str, error: reqwest::Error) -> PyErr {
    let detail = error.to_string();
    if error.is_timeout() {
        return timeout_error(format!("unable to download {url}: {detail}"));
    }
    if error.is_builder() && (detail.contains("builder error") || detail.contains("URL scheme")) {
        return NativeUnsupportedSchemeError::new_err(detail);
    }
    if error.is_connect() {
        let mut source = error.source();
        while let Some(cause) = source {
            let message = cause.to_string().to_ascii_lowercase();
            if message.contains("dns")
                || message.contains("lookup address")
                || message.contains("name or service not known")
                || message.contains("nodename nor servname")
            {
                return NativeCannotResolveHostError::new_err(detail);
            }
            if message.contains("connection refused") {
                return NativeConnectionRefusedError::new_err(detail);
            }
            source = cause.source();
        }
    }
    download_error(format!("unable to download {url}: {detail}"))
}

fn tls_version(value: Option<&str>, setting: &str) -> PyResult<Option<TlsVersion>> {
    value
        .map(|value| match value {
            "TLSv1.0" => Ok(TlsVersion::TLS_1_0),
            "TLSv1.1" => Ok(TlsVersion::TLS_1_1),
            "TLSv1.2" => Ok(TlsVersion::TLS_1_2),
            "TLSv1.3" => Ok(TlsVersion::TLS_1_3),
            _ => Err(PyValueError::new_err(format!(
                "Unknown {setting} value: {value}"
            ))),
        })
        .transpose()
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

fn response_reader(
    response: Response,
    encodings: &[String],
    decode_response: bool,
) -> ResponseReader {
    let stream = response.bytes_stream().map_err(io::Error::other);
    let mut reader: ResponseReader = Box::pin(StreamReader::new(stream));
    if !decode_response
        || !encodings.iter().all(|value| {
            matches!(
                value.as_str(),
                "br" | "deflate" | "gzip" | "identity" | "zstd"
            )
        })
    {
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

fn call_headers_callback(
    callback: Option<&Py<PyAny>>,
    headers: &[(String, Vec<u8>)],
    body_length: Option<u64>,
) -> PyResult<bool> {
    let Some(callback) = callback else {
        return Ok(false);
    };
    Python::attach(|py| {
        let headers = headers
            .iter()
            .map(|(name, value)| (name.clone(), PyBytes::new(py, value).unbind()))
            .collect::<Vec<_>>();
        callback.call1(py, (headers, body_length))?.extract(py)
    })
}

fn call_bytes_callback(callback: Option<&Py<PyAny>>, data: &[u8]) -> PyResult<bool> {
    let Some(callback) = callback else {
        return Ok(false);
    };
    Python::attach(|py| callback.call1(py, (PyBytes::new(py, data),))?.extract(py))
}

#[derive(Default)]
struct DnsCache {
    entries: HashMap<String, Vec<SocketAddr>>,
    order: VecDeque<String>,
}

#[derive(Clone)]
struct CachingResolver {
    enabled: bool,
    limit: usize,
    timeout: Duration,
    allow_ipv6: bool,
    cache: Arc<Mutex<DnsCache>>,
}

impl CachingResolver {
    fn cached(&self, host: &str) -> io::Result<Option<Vec<SocketAddr>>> {
        if !self.enabled {
            return Ok(None);
        }
        let mut cache = self
            .cache
            .lock()
            .map_err(|_| io::Error::other("DNS cache was poisoned"))?;
        let Some(addresses) = cache.entries.get(host).cloned() else {
            return Ok(None);
        };
        cache.order.retain(|item| item != host);
        cache.order.push_back(host.to_owned());
        Ok(Some(addresses))
    }

    fn store(&self, host: String, addresses: Vec<SocketAddr>) -> io::Result<()> {
        if !self.enabled || self.limit == 0 {
            return Ok(());
        }
        let mut cache = self
            .cache
            .lock()
            .map_err(|_| io::Error::other("DNS cache was poisoned"))?;
        cache.order.retain(|item| item != &host);
        cache.order.push_back(host.clone());
        cache.entries.insert(host, addresses);
        while cache.entries.len() > self.limit {
            if let Some(expired) = cache.order.pop_front() {
                cache.entries.remove(&expired);
            }
        }
        Ok(())
    }
}

impl Resolve for CachingResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let resolver = self.clone();
        let host = name.as_str().to_owned();
        Box::pin(async move {
            if let Some(addresses) = resolver.cached(&host)? {
                return Ok(Box::new(addresses.into_iter()) as Addrs);
            }
            let addresses = tokio::time::timeout(
                resolver.timeout,
                tokio::net::lookup_host((host.as_str(), 0)),
            )
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "DNS resolution timed out"))??
            .filter(|address| resolver.allow_ipv6 || address.is_ipv4())
            .collect::<Vec<_>>();
            if addresses.is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("no addresses found for {host}"),
                )
                .into());
            }
            resolver.store(host, addresses.clone())?;
            Ok(Box::new(addresses.into_iter()) as Addrs)
        })
    }
}

#[derive(Clone)]
struct ClientOptions {
    user_agent: Option<String>,
    transport_defaults: bool,
    verify_certificates: bool,
    client_identity: Option<Identity>,
    tls_min_version: Option<TlsVersion>,
    tls_max_version: Option<TlsVersion>,
    http2_enabled: bool,
    bind_address: Option<IpAddr>,
    pool_max_idle_per_host: usize,
    tls_verbose: bool,
    resolver: Arc<CachingResolver>,
}

fn client_builder(options: &ClientOptions) -> ClientBuilder {
    let mut builder = Client::builder()
        .redirect(redirect::Policy::none())
        .no_proxy()
        .danger_accept_invalid_certs(!options.verify_certificates)
        .tls_info(true)
        .connection_verbose(options.tls_verbose)
        .pool_max_idle_per_host(options.pool_max_idle_per_host)
        .dns_resolver(options.resolver.clone());
    if !options.http2_enabled {
        builder = builder.http1_only();
    }
    if let Some(address) = options.bind_address {
        builder = builder.local_address(address);
    }
    if let Some(version) = options.tls_min_version {
        builder = builder.min_tls_version(version);
    }
    if let Some(version) = options.tls_max_version {
        builder = builder.max_tls_version(version);
    }
    if let Some(identity) = options.client_identity.clone() {
        builder = builder.identity(identity);
    }
    if options.transport_defaults {
        let mut default_headers = HeaderMap::new();
        default_headers.insert(
            ACCEPT_ENCODING,
            HeaderValue::from_static("gzip, br, deflate, zstd"),
        );
        builder = builder.default_headers(default_headers);
        if let Some(user_agent) = options
            .user_agent
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            builder = builder.user_agent(user_agent);
        }
    }
    builder
}

fn build_client(options: &ClientOptions, proxy: Option<ProxyConfig<'_>>) -> PyResult<Client> {
    let mut builder = client_builder(options);
    if let Some((url, authorization, credentials)) = proxy {
        let mut proxy_url = reqwest::Url::parse(url)
            .map_err(|error| download_error(format!("invalid proxy URL: {error}")))?;
        if let Some((username, password)) = credentials {
            proxy_url
                .set_username(username)
                .map_err(|_| download_error("invalid SOCKS proxy username"))?;
            proxy_url
                .set_password(Some(password))
                .map_err(|_| download_error("invalid SOCKS proxy password"))?;
        }
        let mut configured = Proxy::all(proxy_url)
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
    credentials: Option<(String, String)>,
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeHttpResponse {
    url: String,
    status: u16,
    headers: Vec<(String, Vec<u8>)>,
    body: Vec<u8>,
    protocol: String,
    latency: f64,
    stopped: bool,
    dataloss: bool,
    warned: bool,
    certificate: Option<Vec<u8>>,
    ip_address: Option<String>,
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

    #[getter]
    fn stopped(&self) -> bool {
        self.stopped
    }

    #[getter]
    fn dataloss(&self) -> bool {
        self.dataloss
    }

    #[getter]
    fn warned(&self) -> bool {
        self.warned
    }

    #[getter]
    fn certificate<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.certificate
            .as_deref()
            .map(|value| PyBytes::new(py, value))
    }

    #[getter]
    fn ip_address(&self) -> Option<&str> {
        self.ip_address.as_deref()
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeHttpClient {
    client: Client,
    proxy_clients: Mutex<HashMap<ProxyClientKey, Client>>,
    options: ClientOptions,
    max_size: usize,
    warn_size: usize,
    fail_on_dataloss: bool,
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
        credentials: Option<(&str, &str)>,
    ) -> PyResult<Client> {
        let Some(proxy) = proxy else {
            return Ok(self.client.clone());
        };
        let key = ProxyClientKey {
            url: proxy.to_owned(),
            authorization: authorization.map(<[u8]>::to_vec),
            credentials: credentials
                .map(|(username, password)| (username.to_owned(), password.to_owned())),
        };
        let mut clients = self.lock_proxy_clients()?;
        if let Some(client) = clients.get(&key) {
            return Ok(client.clone());
        }
        let client = build_client(&self.options, Some((proxy, authorization, credentials)))?;
        clients.insert(key, client.clone());
        Ok(client)
    }
}

#[pymethods]
impl NativeHttpClient {
    #[new]
    #[pyo3(signature = (
        timeout = 180.0,
        max_size = 0,
        warn_size = 0,
        user_agent = None,
        transport_defaults = true,
        verify_certificates = false,
        client_identity = None,
        tls_min_version = None,
        tls_max_version = None,
        http2_enabled = false,
        bind_address = None,
        pool_max_idle_per_host = 8,
        tls_verbose = false,
        dns_cache_enabled = true,
        dns_cache_size = 10000,
        dns_timeout = 60.0,
        allow_ipv6 = false,
        fail_on_dataloss = true
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        timeout: f64,
        max_size: usize,
        warn_size: usize,
        user_agent: Option<&str>,
        transport_defaults: bool,
        verify_certificates: bool,
        client_identity: Option<Vec<u8>>,
        tls_min_version: Option<&str>,
        tls_max_version: Option<&str>,
        http2_enabled: bool,
        bind_address: Option<&str>,
        pool_max_idle_per_host: usize,
        tls_verbose: bool,
        dns_cache_enabled: bool,
        dns_cache_size: usize,
        dns_timeout: f64,
        allow_ipv6: bool,
        fail_on_dataloss: bool,
    ) -> PyResult<Self> {
        let timeout = Duration::try_from_secs_f64(timeout).map_err(|_| {
            PyValueError::new_err("DOWNLOAD_TIMEOUT must be a positive finite number")
        })?;
        if timeout.is_zero() {
            return Err(PyValueError::new_err(
                "DOWNLOAD_TIMEOUT must be a positive finite number",
            ));
        }

        let dns_timeout = Duration::try_from_secs_f64(dns_timeout)
            .map_err(|_| PyValueError::new_err("DNS_TIMEOUT must be a positive finite number"))?;
        if dns_timeout.is_zero() {
            return Err(PyValueError::new_err(
                "DNS_TIMEOUT must be a positive finite number",
            ));
        }
        let options = ClientOptions {
            user_agent: user_agent.map(str::to_owned),
            transport_defaults,
            verify_certificates,
            client_identity: client_identity
                .as_deref()
                .map(Identity::from_pem)
                .transpose()
                .map_err(|error| {
                    PyValueError::new_err(format!("invalid client identity: {error}"))
                })?,
            tls_min_version: tls_version(tls_min_version, "DOWNLOAD_TLS_MIN_VERSION")?,
            tls_max_version: tls_version(tls_max_version, "DOWNLOAD_TLS_MAX_VERSION")?,
            http2_enabled,
            bind_address: bind_address
                .filter(|value| !value.is_empty())
                .map(str::parse)
                .transpose()
                .map_err(|error| {
                    PyValueError::new_err(format!("invalid DOWNLOAD_BIND_ADDRESS: {error}"))
                })?,
            pool_max_idle_per_host,
            tls_verbose,
            resolver: Arc::new(CachingResolver {
                enabled: dns_cache_enabled,
                limit: dns_cache_size,
                timeout: dns_timeout,
                allow_ipv6,
                cache: Arc::new(Mutex::new(DnsCache::default())),
            }),
        };
        let client = build_client(&options, None)?;
        Ok(Self {
            client,
            proxy_clients: Mutex::new(HashMap::new()),
            options,
            max_size,
            warn_size,
            fail_on_dataloss,
            timeout,
        })
    }

    #[pyo3(signature = (
        url,
        method,
        headers,
        body,
        proxy = None,
        headers_callback = None,
        bytes_callback = None,
        request_timeout = None,
        request_max_size = None,
        request_warn_size = None,
        request_fail_on_dataloss = None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn fetch<'py>(
        &self,
        py: Python<'py>,
        url: String,
        method: String,
        headers: Vec<(String, Vec<u8>)>,
        body: Vec<u8>,
        proxy: Option<OwnedProxyConfig>,
        headers_callback: Option<Py<PyAny>>,
        bytes_callback: Option<Py<PyAny>>,
        request_timeout: Option<f64>,
        request_max_size: Option<usize>,
        request_warn_size: Option<usize>,
        request_fail_on_dataloss: Option<bool>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client_for_proxy(
            proxy.as_ref().map(|(url, _, _)| url.as_str()),
            proxy
                .as_ref()
                .and_then(|(_, authorization, _)| authorization.as_deref()),
            proxy
                .as_ref()
                .and_then(|(_, _, credentials)| credentials.as_ref())
                .map(|(username, password)| (username.as_str(), password.as_str())),
        )?;
        let max_size = request_max_size.unwrap_or(self.max_size);
        let warn_size = request_warn_size.unwrap_or(self.warn_size);
        let fail_on_dataloss = request_fail_on_dataloss.unwrap_or(self.fail_on_dataloss);
        let decode_response = self.options.transport_defaults;
        let timeout = match request_timeout {
            Some(value) => {
                let timeout = Duration::try_from_secs_f64(value).map_err(|_| {
                    PyValueError::new_err("download_timeout must be a positive finite number")
                })?;
                if timeout.is_zero() {
                    return Err(PyValueError::new_err(
                        "download_timeout must be a positive finite number",
                    ));
                }
                timeout
            }
            None => self.timeout,
        };
        crate::runtime::future_into_py(py, async move {
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

            let request = client
                .request(parsed_method, &url)
                .headers(parsed_headers)
                .body(body);
            let started = Instant::now();
            let response = tokio::time::timeout(timeout, request.send())
                .await
                .map_err(|_| {
                    timeout_error(format!(
                        "unable to download {url}: timed out after {} seconds",
                        timeout.as_secs_f64()
                    ))
                })?
                .map_err(|error| request_error(&url, error))?;
            let latency = started.elapsed().as_secs_f64();

            let final_url = response.url().to_string();
            let status = response.status().as_u16();
            let protocol = protocol_name(response.version());
            let ip_address = response
                .remote_addr()
                .map(|address| address.ip().to_string());
            let certificate = response
                .extensions()
                .get::<TlsInfo>()
                .and_then(TlsInfo::peer_certificate)
                .map(<[u8]>::to_vec);
            let encodings = content_encodings(response.headers());
            let response_headers = response
                .headers()
                .iter()
                .map(|(name, value)| (name.as_str().to_owned(), value.as_bytes().to_vec()))
                .collect::<Vec<_>>();
            let mut stopped = call_headers_callback(
                headers_callback.as_ref(),
                &response_headers,
                response.content_length(),
            )?;
            let mut warned = response
                .content_length()
                .is_some_and(|size| warn_size != 0 && size > warn_size as u64);
            if !stopped
                && let Some(declared_size) = response.content_length()
                && max_size != 0
                && declared_size > max_size as u64
            {
                return Err(cancelled_error(format!(
                    "response exceeded DOWNLOAD_MAXSIZE ({max_size} bytes)"
                )));
            }

            let mut reader = response_reader(response, &encodings, decode_response);
            let mut response_body = Vec::new();
            let mut chunk = [0_u8; 16 * 1024];
            let mut dataloss = false;
            while !stopped {
                let bytes_read = match tokio::time::timeout(timeout, reader.read(&mut chunk)).await
                {
                    Err(_) => {
                        return Err(timeout_error(format!(
                            "unable to read response body from {final_url}: timed out after {} seconds",
                            timeout.as_secs_f64()
                        )));
                    }
                    Ok(Err(error)) if fail_on_dataloss => {
                        return Err(data_loss_error(format!(
                            "response body from {final_url} was incomplete: {error}"
                        )));
                    }
                    Ok(Err(_)) => {
                        dataloss = true;
                        break;
                    }
                    Ok(Ok(bytes_read)) => bytes_read,
                };
                if bytes_read == 0 {
                    break;
                }
                let next_size = response_body
                    .len()
                    .checked_add(bytes_read)
                    .ok_or_else(|| download_error("response body size overflowed"))?;
                response_body.extend_from_slice(&chunk[..bytes_read]);
                if warn_size != 0 && next_size > warn_size {
                    warned = true;
                }
                stopped = call_bytes_callback(bytes_callback.as_ref(), &chunk[..bytes_read])?;
                if !stopped && max_size != 0 && next_size > max_size {
                    return Err(cancelled_error(format!(
                        "response exceeded DOWNLOAD_MAXSIZE ({max_size} bytes)"
                    )));
                }
            }

            Python::attach(|py| {
                Py::new(
                    py,
                    NativeHttpResponse {
                        url: final_url,
                        status,
                        headers: response_headers,
                        body: response_body,
                        protocol,
                        latency,
                        stopped,
                        dataloss,
                        warned,
                        certificate,
                        ip_address,
                    },
                )
            })
        })
    }

    #[getter]
    fn proxy_client_count(&self) -> PyResult<usize> {
        Ok(self.lock_proxy_clients()?.len())
    }

    #[getter]
    fn dns_cache_size(&self) -> PyResult<usize> {
        let cache = self
            .options
            .resolver
            .cache
            .lock()
            .map_err(|_| PyRuntimeError::new_err("DNS cache was poisoned"))?;
        Ok(cache.entries.len())
    }
}
