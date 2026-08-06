use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashSet};

mod downloader;
mod engine;
mod policy;

use downloader::{NativeHttpClient, NativeHttpResponse};
use engine::NativeCrawlCoordinator;
use policy::{NativePolicyRuntime, NativeRetryDecision};
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};
use sha2::{Digest, Sha256};
use url::{Url, form_urlencoded};

pyo3::create_exception!(_native, NativeDownloadError, pyo3::exceptions::PyException);

type RequestTuple = (String, String, Vec<u8>, i64);

fn canonicalize_url(url: &str) -> PyResult<String> {
    let mut parsed =
        Url::parse(url).map_err(|error| PyValueError::new_err(format!("invalid URL: {error}")))?;
    let scheme = parsed.scheme().to_lowercase();
    if parsed.host_str().is_none() {
        return Err(PyValueError::new_err(
            "URL must include a scheme and hostname",
        ));
    }

    parsed
        .set_scheme(&scheme)
        .map_err(|_| PyValueError::new_err("invalid URL scheme"))?;
    parsed.set_fragment(None);
    if (scheme == "http" && parsed.port() == Some(80))
        || (scheme == "https" && parsed.port() == Some(443))
    {
        parsed
            .set_port(None)
            .map_err(|_| PyValueError::new_err("unable to normalize URL port"))?;
    }
    if parsed.path().is_empty() {
        parsed.set_path("/");
    }

    let mut pairs: Vec<(String, String)> = parsed
        .query_pairs()
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    pairs.sort();
    if pairs.is_empty() {
        parsed.set_query(None);
    } else {
        let mut serializer = form_urlencoded::Serializer::new(String::new());
        serializer.extend_pairs(pairs);
        parsed.set_query(Some(&serializer.finish()));
    }
    Ok(parsed.into())
}

fn fingerprint_bytes(url: &str, method: &str, body: &[u8]) -> PyResult<[u8; 32]> {
    let normalized_method = method.trim().to_uppercase();
    let canonical_url = canonicalize_url(url)?;
    let mut digest = Sha256::new();
    digest.update(normalized_method.as_bytes());
    digest.update([0]);
    digest.update(canonical_url.as_bytes());
    digest.update([0]);
    digest.update(body);
    Ok(digest.finalize().into())
}

#[pyfunction]
fn fingerprint<'py>(
    py: Python<'py>,
    url: &str,
    method: &str,
    body: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    Ok(PyBytes::new(py, &fingerprint_bytes(url, method, body)?))
}

#[pyfunction]
fn fingerprint_batch(py: Python<'_>, requests: Vec<RequestTuple>) -> PyResult<Vec<Py<PyBytes>>> {
    requests
        .iter()
        .map(|(url, method, body, _)| {
            let value = fingerprint_bytes(url, method, body)?;
            Ok(PyBytes::new(py, &value).unbind())
        })
        .collect()
}

#[pyclass(module = "spideroxide._native")]
struct RustDupeFilter {
    fingerprints: HashSet<[u8; 32]>,
}

#[pymethods]
impl RustDupeFilter {
    #[new]
    fn new() -> Self {
        Self {
            fingerprints: HashSet::new(),
        }
    }

    #[pyo3(signature = (url, method = None, body = None))]
    fn seen(&mut self, url: &str, method: Option<&str>, body: Option<&[u8]>) -> PyResult<bool> {
        let value = fingerprint_bytes(url, method.unwrap_or("GET"), body.unwrap_or_default())?;
        Ok(!self.fingerprints.insert(value))
    }

    fn seen_batch(&mut self, requests: Vec<RequestTuple>) -> PyResult<Vec<bool>> {
        requests
            .iter()
            .map(|(url, method, body, _)| self.seen(url, Some(method), Some(body)))
            .collect()
    }

    fn __len__(&self) -> usize {
        self.fingerprints.len()
    }
}

#[pyclass(module = "spideroxide._native")]
#[derive(Clone, Debug, Eq, PartialEq)]
struct Request {
    url: String,
    method: String,
    body: Vec<u8>,
    priority: i64,
}

#[pymethods]
impl Request {
    #[getter]
    fn url(&self) -> &str {
        &self.url
    }

    #[getter]
    fn method(&self) -> &str {
        &self.method
    }

    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body)
    }

    #[getter]
    fn priority(&self) -> i64 {
        self.priority
    }

    fn __repr__(&self) -> String {
        format!(
            "Request(url={:?}, method={:?}, body=<{} bytes>, priority={})",
            self.url,
            self.method,
            self.body.len(),
            self.priority
        )
    }
}

#[derive(Debug, Eq, PartialEq)]
struct QueueEntry {
    priority: i64,
    sequence: u64,
    request: Request,
}

impl Ord for QueueEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        self.priority
            .cmp(&other.priority)
            // BinaryHeap returns the greatest item, so an earlier sequence compares greater.
            .then_with(|| other.sequence.cmp(&self.sequence))
    }
}

impl PartialOrd for QueueEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[pyclass(module = "spideroxide._native")]
struct RustScheduler {
    fingerprints: HashSet<[u8; 32]>,
    queue: BinaryHeap<QueueEntry>,
    next_sequence: u64,
}

impl RustScheduler {
    fn push_inner(&mut self, request: RequestTuple, filter_duplicates: bool) -> PyResult<bool> {
        let (url, method, body, priority) = request;
        let value = fingerprint_bytes(&url, &method, &body)?;
        if filter_duplicates && !self.fingerprints.insert(value) {
            return Ok(false);
        }
        let sequence = self.next_sequence;
        self.next_sequence = self
            .next_sequence
            .checked_add(1)
            .ok_or_else(|| PyOverflowError::new_err("scheduler sequence exhausted"))?;
        self.queue.push(QueueEntry {
            priority,
            sequence,
            request: Request {
                url,
                method,
                body,
                priority,
            },
        });
        Ok(true)
    }

    fn pop_inner(&mut self) -> Option<Request> {
        self.queue.pop().map(|entry| entry.request)
    }
}

#[pymethods]
impl RustScheduler {
    #[new]
    fn new() -> Self {
        Self {
            fingerprints: HashSet::new(),
            queue: BinaryHeap::new(),
            next_sequence: 0,
        }
    }

    #[pyo3(signature = (url, method = None, body = None, priority = 0))]
    fn push(
        &mut self,
        url: String,
        method: Option<String>,
        body: Option<Vec<u8>>,
        priority: i64,
    ) -> PyResult<bool> {
        self.push_inner(
            (
                url,
                method.unwrap_or_else(|| "GET".to_owned()),
                body.unwrap_or_default(),
                priority,
            ),
            true,
        )
    }

    #[pyo3(signature = (url, method = None, body = None, priority = 0))]
    fn push_unchecked(
        &mut self,
        url: String,
        method: Option<String>,
        body: Option<Vec<u8>>,
        priority: i64,
    ) -> PyResult<bool> {
        self.push_inner(
            (
                url,
                method.unwrap_or_else(|| "GET".to_owned()),
                body.unwrap_or_default(),
                priority,
            ),
            false,
        )
    }

    fn push_batch(&mut self, requests: Vec<RequestTuple>) -> PyResult<Vec<bool>> {
        requests
            .into_iter()
            .map(|request| self.push_inner(request, true))
            .collect()
    }

    fn pop(&mut self) -> Option<Request> {
        self.pop_inner()
    }

    fn pop_batch(&mut self, py: Python<'_>, count: usize) -> PyResult<Vec<Py<Request>>> {
        let take = count.min(self.queue.len());
        (0..take)
            .map(|_| {
                let request = self
                    .pop_inner()
                    .expect("queue length was checked before popping");
                Py::new(py, request)
            })
            .collect()
    }

    fn __len__(&self) -> usize {
        self.queue.len()
    }
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(fingerprint, module)?)?;
    module.add_function(wrap_pyfunction!(fingerprint_batch, module)?)?;
    module.add(
        "NativeDownloadError",
        module.py().get_type::<NativeDownloadError>(),
    )?;
    module.add_class::<NativeHttpClient>()?;
    module.add_class::<NativeHttpResponse>()?;
    module.add_class::<NativeCrawlCoordinator>()?;
    module.add_class::<NativePolicyRuntime>()?;
    module.add_class::<NativeRetryDecision>()?;
    module.add_class::<Request>()?;
    module.add_class::<RustDupeFilter>()?;
    module.add_class::<RustScheduler>()?;
    Ok(())
}
