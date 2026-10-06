use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashSet};

mod cookies;
mod depth;
mod downloader;
mod engine;
mod fingerprints;
mod httpcache;
mod job;
mod links;
mod media;
mod policy;
mod robots;
mod runtime;
mod slots;

use cookies::NativeCookieJar;
use depth::{NativeDepthDecision, NativeDepthPolicy};
use downloader::{NativeHttpClient, NativeHttpResponse};
use engine::NativeCrawlCoordinator;
use fingerprints::{
    RequestFingerprint, fingerprint, fingerprint_batch, fingerprint_bytes, py_canonicalize_url,
};
use httpcache::NativeHttpCacheStore;
use links::extract_link_candidates;
use media::NativeMediaStore;
use policy::{NativePolicyRuntime, NativeRetryDecision};
use pyo3::exceptions::PyOverflowError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};
use robots::{NativeRobotParser, NativeRobotsDecision, NativeRobotsRuntime};
use runtime::shutdown_async_runtime;
use slots::{NativeDownloadSlotLease, NativeDownloadSlotManager};

pyo3::create_exception!(_native, NativeDownloadError, pyo3::exceptions::PyException);
pyo3::create_exception!(_native, NativeDownloadCancelledError, NativeDownloadError);
pyo3::create_exception!(_native, NativeDownloadTimeoutError, NativeDownloadError);
pyo3::create_exception!(_native, NativeCannotResolveHostError, NativeDownloadError);
pyo3::create_exception!(_native, NativeConnectionRefusedError, NativeDownloadError);
pyo3::create_exception!(_native, NativeUnsupportedSchemeError, NativeDownloadError);
pyo3::create_exception!(_native, NativeResponseDataLossError, NativeDownloadError);

type RequestTuple = (String, String, Vec<u8>, i64);

#[pyclass(module = "spideroxide._native")]
struct RustDupeFilter {
    fingerprints: HashSet<RequestFingerprint>,
}

#[pymethods]
impl RustDupeFilter {
    #[new]
    fn new() -> Self {
        Self {
            fingerprints: HashSet::new(),
        }
    }

    #[pyo3(signature = (url, method = None, body = None, verbatim_url = false))]
    fn seen(
        &mut self,
        url: &str,
        method: Option<&str>,
        body: Option<&[u8]>,
        verbatim_url: bool,
    ) -> PyResult<bool> {
        let value = fingerprint_bytes(
            url,
            method.unwrap_or("GET"),
            body.unwrap_or_default(),
            &[],
            false,
            verbatim_url,
        )?;
        Ok(!self.fingerprints.insert(value))
    }

    fn seen_batch(&mut self, requests: Vec<RequestTuple>) -> PyResult<Vec<bool>> {
        requests
            .iter()
            .map(|(url, method, body, _)| self.seen(url, Some(method), Some(body), false))
            .collect()
    }

    fn __len__(&self) -> usize {
        self.fingerprints.len()
    }
}

#[pyclass(module = "spideroxide._native", skip_from_py_object)]
#[derive(Clone, Debug, Eq, PartialEq)]
struct Request {
    url: String,
    method: String,
    body: Vec<u8>,
    priority: i64,
    sequence: u64,
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

    #[getter]
    fn sequence(&self) -> u64 {
        self.sequence
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
    fingerprints: HashSet<RequestFingerprint>,
    queue: BinaryHeap<QueueEntry>,
    next_sequence: u64,
}

impl RustScheduler {
    fn push_inner(
        &mut self,
        request: RequestTuple,
        filter_duplicates: bool,
        verbatim_url: bool,
    ) -> PyResult<bool> {
        let (url, method, body, priority) = request;
        let value = fingerprint_bytes(&url, &method, &body, &[], false, verbatim_url)?;
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
                sequence,
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

    #[pyo3(signature = (
        url,
        method = None,
        body = None,
        priority = 0,
        verbatim_url = false
    ))]
    fn push(
        &mut self,
        url: String,
        method: Option<String>,
        body: Option<Vec<u8>>,
        priority: i64,
        verbatim_url: bool,
    ) -> PyResult<bool> {
        self.push_inner(
            (
                url,
                method.unwrap_or_else(|| "GET".to_owned()),
                body.unwrap_or_default(),
                priority,
            ),
            true,
            verbatim_url,
        )
    }

    #[pyo3(signature = (
        url,
        method = None,
        body = None,
        priority = 0,
        verbatim_url = false
    ))]
    fn push_unchecked(
        &mut self,
        url: String,
        method: Option<String>,
        body: Option<Vec<u8>>,
        priority: i64,
        verbatim_url: bool,
    ) -> PyResult<bool> {
        self.push_inner(
            (
                url,
                method.unwrap_or_else(|| "GET".to_owned()),
                body.unwrap_or_default(),
                priority,
            ),
            false,
            verbatim_url,
        )
    }

    fn push_batch(&mut self, requests: Vec<RequestTuple>) -> PyResult<Vec<bool>> {
        requests
            .into_iter()
            .map(|request| self.push_inner(request, true, false))
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
    module.add_function(wrap_pyfunction!(py_canonicalize_url, module)?)?;
    module.add_function(wrap_pyfunction!(extract_link_candidates, module)?)?;
    module.add_function(wrap_pyfunction!(shutdown_async_runtime, module)?)?;
    module.add(
        "NativeDownloadError",
        module.py().get_type::<NativeDownloadError>(),
    )?;
    module.add(
        "NativeDownloadCancelledError",
        module.py().get_type::<NativeDownloadCancelledError>(),
    )?;
    module.add(
        "NativeDownloadTimeoutError",
        module.py().get_type::<NativeDownloadTimeoutError>(),
    )?;
    module.add(
        "NativeCannotResolveHostError",
        module.py().get_type::<NativeCannotResolveHostError>(),
    )?;
    module.add(
        "NativeConnectionRefusedError",
        module.py().get_type::<NativeConnectionRefusedError>(),
    )?;
    module.add(
        "NativeUnsupportedSchemeError",
        module.py().get_type::<NativeUnsupportedSchemeError>(),
    )?;
    module.add(
        "NativeResponseDataLossError",
        module.py().get_type::<NativeResponseDataLossError>(),
    )?;
    module.add_class::<NativeHttpClient>()?;
    module.add_class::<NativeHttpResponse>()?;
    module.add_class::<NativeHttpCacheStore>()?;
    module.add_class::<NativeMediaStore>()?;
    module.add_class::<NativeCookieJar>()?;
    module.add_class::<NativeDepthPolicy>()?;
    module.add_class::<NativeDepthDecision>()?;
    module.add_class::<NativeCrawlCoordinator>()?;
    module.add_class::<NativePolicyRuntime>()?;
    module.add_class::<NativeRetryDecision>()?;
    module.add_class::<NativeDownloadSlotManager>()?;
    module.add_class::<NativeDownloadSlotLease>()?;
    module.add_class::<NativeRobotsRuntime>()?;
    module.add_class::<NativeRobotsDecision>()?;
    module.add_class::<NativeRobotParser>()?;
    module.add_class::<Request>()?;
    module.add_class::<RustDupeFilter>()?;
    module.add_class::<RustScheduler>()?;
    module
        .py()
        .import("atexit")?
        .call_method1("register", (module.getattr("_shutdown_async_runtime")?,))?;
    Ok(())
}
