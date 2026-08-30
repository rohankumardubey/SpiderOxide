use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::mem;
use std::str::FromStr;
use std::sync::{Arc, Mutex, MutexGuard};

use num_bigint::BigInt;
use pyo3::exceptions::{PyOverflowError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use tokio::sync::Notify;

use crate::fingerprint_bytes;
use crate::job::PersistentJobStore;

#[derive(Debug, Eq, PartialEq)]
struct CoordinatorQueueEntry {
    priority: BigInt,
    sequence: u64,
    request_id: u64,
}

impl Ord for CoordinatorQueueEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        self.priority
            .cmp(&other.priority)
            .then_with(|| other.sequence.cmp(&self.sequence))
    }
}

impl PartialOrd for CoordinatorQueueEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

struct CoordinatorState {
    fingerprints: HashSet<[u8; 32]>,
    staged: HashMap<u64, CoordinatorQueueEntry>,
    queue: BinaryHeap<CoordinatorQueueEntry>,
    active: HashSet<u64>,
    next_request_id: u64,
    next_sequence: u64,
    recovered: Vec<(u64, Vec<u8>)>,
    persistent_ids: HashSet<u64>,
    job_store: Option<PersistentJobStore>,
    persistence_enabled: bool,
    input_closed: bool,
    finished: bool,
    aborted: bool,
}

impl CoordinatorState {
    fn new(job_dir: Option<&str>) -> PyResult<Self> {
        let (job_store, recovered_requests, fingerprints) = if let Some(path) = job_dir {
            let store = PersistentJobStore::open(path)?;
            let requests = store.load_requests()?;
            let fingerprints = store.load_fingerprints()?;
            (Some(store), requests, fingerprints)
        } else {
            (None, Vec::new(), HashSet::new())
        };
        let mut queue = BinaryHeap::new();
        let mut recovered = Vec::with_capacity(recovered_requests.len());
        let mut persistent_ids = HashSet::with_capacity(recovered_requests.len());
        let mut next_request_id = 0;
        let mut next_sequence = 0;
        for request in recovered_requests {
            let priority = BigInt::from_str(&request.priority).map_err(|_| {
                PyRuntimeError::new_err(format!(
                    "persistent request {} has an invalid priority",
                    request.request_id
                ))
            })?;
            next_request_id = next_request_id.max(
                request
                    .request_id
                    .checked_add(1)
                    .ok_or_else(|| PyOverflowError::new_err("request identifier exhausted"))?,
            );
            next_sequence = next_sequence.max(
                request
                    .sequence
                    .checked_add(1)
                    .ok_or_else(|| PyOverflowError::new_err("scheduler sequence exhausted"))?,
            );
            queue.push(CoordinatorQueueEntry {
                priority,
                sequence: request.sequence,
                request_id: request.request_id,
            });
            persistent_ids.insert(request.request_id);
            recovered.push((request.request_id, request.payload));
        }
        let persistence_enabled = job_store.is_some();
        Ok(Self {
            fingerprints,
            staged: HashMap::new(),
            queue,
            active: HashSet::new(),
            next_request_id,
            next_sequence,
            recovered,
            persistent_ids,
            job_store,
            persistence_enabled,
            input_closed: false,
            finished: false,
            aborted: false,
        })
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeCrawlCoordinator {
    state: Arc<Mutex<CoordinatorState>>,
    notify: Arc<Notify>,
    concurrency: usize,
    pending_limit: usize,
}

impl NativeCrawlCoordinator {
    fn lock_state(&self) -> PyResult<MutexGuard<'_, CoordinatorState>> {
        self.state
            .lock()
            .map_err(|_| PyRuntimeError::new_err("native crawl coordinator state was poisoned"))
    }
}

#[pymethods]
impl NativeCrawlCoordinator {
    #[new]
    #[pyo3(signature = (concurrency, pending_limit, job_dir = None))]
    fn new(concurrency: usize, pending_limit: usize, job_dir: Option<&str>) -> PyResult<Self> {
        if concurrency == 0 {
            return Err(PyValueError::new_err("concurrency must be at least 1"));
        }
        if pending_limit == 0 {
            return Err(PyValueError::new_err("pending limit must be at least 1"));
        }
        Ok(Self {
            state: Arc::new(Mutex::new(CoordinatorState::new(job_dir)?)),
            notify: Arc::new(Notify::new()),
            concurrency,
            pending_limit,
        })
    }

    #[pyo3(signature = (
        url,
        method,
        body,
        priority = "0",
        filter_duplicates = true,
        payload = None
    ))]
    fn schedule(
        &self,
        url: &str,
        method: &str,
        body: &[u8],
        priority: &str,
        filter_duplicates: bool,
        payload: Option<&[u8]>,
    ) -> PyResult<Option<u64>> {
        let fingerprint = fingerprint_bytes(url, method, body)?;
        let priority = BigInt::from_str(priority)
            .map_err(|_| PyValueError::new_err("priority must be an integer"))?;
        let request_id;
        {
            let mut state = self.lock_state()?;
            if state.aborted {
                return Err(PyRuntimeError::new_err(
                    "native crawl coordinator was aborted",
                ));
            }
            if state.finished {
                return Err(PyRuntimeError::new_err(
                    "native crawl coordinator is finished",
                ));
            }
            if filter_duplicates && state.fingerprints.contains(&fingerprint) {
                return Ok(None);
            }

            request_id = state.next_request_id;
            state.next_request_id = state
                .next_request_id
                .checked_add(1)
                .ok_or_else(|| PyOverflowError::new_err("request identifier exhausted"))?;
            let sequence = state.next_sequence;
            state.next_sequence = state
                .next_sequence
                .checked_add(1)
                .ok_or_else(|| PyOverflowError::new_err("scheduler sequence exhausted"))?;
            let priority_text = priority.to_string();
            let persisted = state.persistence_enabled && payload.is_some();
            if let Some(store) = state.job_store.as_mut()
                && !store.schedule(
                    request_id,
                    sequence,
                    &priority_text,
                    payload,
                    filter_duplicates.then_some(&fingerprint),
                )?
            {
                return Ok(None);
            }
            if filter_duplicates {
                state.fingerprints.insert(fingerprint);
            }
            if persisted {
                state.persistent_ids.insert(request_id);
            }
            state.staged.insert(
                request_id,
                CoordinatorQueueEntry {
                    priority,
                    sequence,
                    request_id,
                },
            );
        }
        Ok(Some(request_id))
    }

    fn activate(&self, request_id: u64) -> PyResult<()> {
        {
            let mut state = self.lock_state()?;
            let entry = state.staged.remove(&request_id).ok_or_else(|| {
                PyValueError::new_err(format!("request {request_id} is not staged"))
            })?;
            state.queue.push(entry);
        }
        self.notify.notify_waiters();
        Ok(())
    }

    fn next_request<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = self.state.clone();
        let notify = self.notify.clone();
        let concurrency = self.concurrency;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            loop {
                let notified = notify.notified();
                let mut popped = None;
                {
                    let mut current = state.lock().map_err(|_| {
                        PyRuntimeError::new_err("native crawl coordinator state was poisoned")
                    })?;
                    if current.aborted {
                        return Ok(None);
                    }
                    if current.active.len() < concurrency
                        && let Some(entry) = current.queue.pop()
                    {
                        current.active.insert(entry.request_id);
                        popped = Some(entry.request_id);
                    }
                    if popped.is_none()
                        && current.input_closed
                        && current.staged.is_empty()
                        && current.queue.is_empty()
                        && current.active.is_empty()
                    {
                        current.finished = true;
                        return Ok(None);
                    }
                }
                if let Some(request_id) = popped {
                    notify.notify_waiters();
                    return Ok(Some(request_id));
                }
                notified.await;
            }
        })
    }

    fn wait_for_pending_slot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = self.state.clone();
        let notify = self.notify.clone();
        let pending_limit = self.pending_limit;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            loop {
                let notified = notify.notified();
                {
                    let current = state.lock().map_err(|_| {
                        PyRuntimeError::new_err("native crawl coordinator state was poisoned")
                    })?;
                    if current.aborted || current.finished {
                        return Ok(false);
                    }
                    if current.queue.len() + current.staged.len() < pending_limit {
                        return Ok(true);
                    }
                }
                notified.await;
            }
        })
    }

    fn complete(&self, request_id: u64) -> PyResult<()> {
        {
            let mut state = self.lock_state()?;
            if !state.active.contains(&request_id) {
                return Err(PyValueError::new_err(format!(
                    "request {request_id} is not active"
                )));
            }
            if let Some(store) = state.job_store.as_mut() {
                store.complete(request_id)?;
            }
            state.active.remove(&request_id);
            state.persistent_ids.remove(&request_id);
        }
        self.notify.notify_waiters();
        Ok(())
    }

    fn release(&self, request_id: u64) -> PyResult<()> {
        {
            let mut state = self.lock_state()?;
            if !state.active.remove(&request_id) {
                return Err(PyValueError::new_err(format!(
                    "request {request_id} is not active"
                )));
            }
        }
        self.notify.notify_waiters();
        Ok(())
    }

    fn close_input(&self) -> PyResult<()> {
        {
            let mut state = self.lock_state()?;
            state.input_closed = true;
        }
        self.notify.notify_waiters();
        Ok(())
    }

    fn abort(&self) -> PyResult<()> {
        {
            let mut state = self.lock_state()?;
            state.aborted = true;
            state.staged.clear();
            state.queue.clear();
        }
        self.notify.notify_waiters();
        Ok(())
    }

    fn take_recovered(&self) -> PyResult<Vec<(u64, Vec<u8>)>> {
        Ok(mem::take(&mut self.lock_state()?.recovered))
    }

    fn is_persistent(&self, request_id: u64) -> PyResult<bool> {
        Ok(self.lock_state()?.persistent_ids.contains(&request_id))
    }

    fn load_spider_state(&self) -> PyResult<Option<Vec<u8>>> {
        let state = self.lock_state()?;
        match state.job_store.as_ref() {
            Some(store) => store.load_spider_state(),
            None => Ok(None),
        }
    }

    fn save_spider_state(&self, payload: &[u8]) -> PyResult<bool> {
        let mut state = self.lock_state()?;
        match state.job_store.as_mut() {
            Some(store) => {
                store.save_spider_state(payload)?;
                Ok(true)
            }
            None => Ok(false),
        }
    }

    fn close(&self) -> PyResult<()> {
        let store = {
            let mut state = self.lock_state()?;
            state.aborted = true;
            state.staged.clear();
            state.queue.clear();
            state.job_store.take()
        };
        self.notify.notify_waiters();
        if let Some(store) = store {
            store.close()?;
        }
        Ok(())
    }

    #[getter]
    fn queued_count(&self) -> PyResult<usize> {
        let state = self.lock_state()?;
        Ok(state.queue.len() + state.staged.len())
    }

    #[getter]
    fn active_count(&self) -> PyResult<usize> {
        Ok(self.lock_state()?.active.len())
    }

    #[getter]
    fn seen_count(&self) -> PyResult<usize> {
        Ok(self.lock_state()?.fingerprints.len())
    }

    #[getter]
    fn recovered_count(&self) -> PyResult<usize> {
        Ok(self.lock_state()?.recovered.len())
    }

    #[getter]
    fn persistent(&self) -> PyResult<bool> {
        Ok(self.lock_state()?.persistence_enabled)
    }
}
