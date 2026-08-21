use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyOverflowError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use rand::Rng;
use tokio::sync::Notify;

fn validate_duration(name: &str, value: f64) -> PyResult<()> {
    if !value.is_finite() || value < 0.0 || Duration::try_from_secs_f64(value).is_err() {
        return Err(PyValueError::new_err(format!(
            "{name} must be a finite non-negative duration"
        )));
    }
    Ok(())
}

fn validate_randomized_duration(name: &str, value: f64, randomize: bool) -> PyResult<()> {
    if randomize {
        validate_duration(name, value * 1.5)?;
    }
    Ok(())
}

struct DownloadSlot {
    concurrency: usize,
    delay: f64,
    randomize_delay: bool,
    active: HashSet<u64>,
    waiting: usize,
    last_start: Option<Instant>,
    last_activity: Instant,
    next_delay: f64,
    last_latency: Option<f64>,
}

impl DownloadSlot {
    fn new(concurrency: usize, delay: f64, randomize_delay: bool) -> Self {
        Self {
            concurrency,
            delay,
            randomize_delay,
            active: HashSet::new(),
            waiting: 0,
            last_start: None,
            last_activity: Instant::now(),
            next_delay: 0.0,
            last_latency: None,
        }
    }

    fn sample_delay(&self) -> f64 {
        if self.randomize_delay && self.delay > 0.0 {
            rand::rng().random_range(0.5 * self.delay..1.5 * self.delay)
        } else {
            self.delay
        }
    }
}

struct DownloadSlotsState {
    slots: HashMap<String, DownloadSlot>,
    next_lease_id: u64,
    stats: HashMap<String, u64>,
    pending_stats: HashMap<String, u64>,
    closed: bool,
}

impl DownloadSlotsState {
    fn increment(&mut self, key: &str) {
        *self.stats.entry(key.to_owned()).or_default() += 1;
        *self.pending_stats.entry(key.to_owned()).or_default() += 1;
    }

    fn prune_inactive(&mut self, now: Instant, max_idle: Duration) {
        self.slots.retain(|_, slot| {
            !slot.active.is_empty()
                || slot.waiting > 0
                || now.duration_since(slot.last_activity) <= max_idle
        });
    }
}

struct SlotWaiter {
    state: Arc<Mutex<DownloadSlotsState>>,
    notify: Arc<Notify>,
    key: String,
    registered: bool,
}

impl Drop for SlotWaiter {
    fn drop(&mut self) {
        if !self.registered {
            return;
        }
        if let Ok(mut state) = self.state.lock()
            && let Some(slot) = state.slots.get_mut(&self.key)
        {
            slot.waiting = slot.waiting.saturating_sub(1);
            slot.last_activity = Instant::now();
        }
        self.notify.notify_waiters();
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeDownloadSlotLease {
    state: Arc<Mutex<DownloadSlotsState>>,
    notify: Arc<Notify>,
    key: String,
    lease_id: u64,
    released: bool,
}

impl Drop for NativeDownloadSlotLease {
    fn drop(&mut self) {
        if self.released {
            return;
        }
        if let Ok(mut state) = self.state.lock() {
            let removed = state.slots.get_mut(&self.key).is_some_and(|slot| {
                let removed = slot.active.remove(&self.lease_id);
                if removed {
                    slot.last_activity = Instant::now();
                }
                removed
            });
            if removed {
                state.increment("downloader/slot/released");
                state.increment("downloader/slot/cancelled");
            }
        }
        self.notify.notify_waiters();
    }
}

#[pymethods]
impl NativeDownloadSlotLease {
    #[getter]
    fn key(&self) -> &str {
        &self.key
    }

    #[getter]
    fn lease_id(&self) -> u64 {
        self.lease_id
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeDownloadSlotManager {
    state: Arc<Mutex<DownloadSlotsState>>,
    notify: Arc<Notify>,
    default_concurrency: usize,
    default_delay: f64,
    randomize_delay: bool,
    autothrottle_enabled: bool,
    min_delay: f64,
    max_delay: f64,
    target_concurrency: f64,
}

impl NativeDownloadSlotManager {
    fn lock_state(&self) -> PyResult<MutexGuard<'_, DownloadSlotsState>> {
        self.state
            .lock()
            .map_err(|_| PyRuntimeError::new_err("native download slot state was poisoned"))
    }
}

#[pymethods]
impl NativeDownloadSlotManager {
    #[new]
    #[pyo3(signature = (
        concurrency,
        delay = 0.0,
        randomize_delay = true,
        autothrottle_enabled = false,
        autothrottle_start_delay = 5.0,
        autothrottle_max_delay = 60.0,
        autothrottle_target_concurrency = 1.0
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        concurrency: usize,
        delay: f64,
        randomize_delay: bool,
        autothrottle_enabled: bool,
        autothrottle_start_delay: f64,
        autothrottle_max_delay: f64,
        autothrottle_target_concurrency: f64,
    ) -> PyResult<Self> {
        if concurrency == 0 {
            return Err(PyValueError::new_err("slot concurrency must be at least 1"));
        }
        validate_duration("download delay", delay)?;
        if autothrottle_enabled {
            for (name, value) in [
                ("AutoThrottle start delay", autothrottle_start_delay),
                ("AutoThrottle maximum delay", autothrottle_max_delay),
            ] {
                validate_duration(name, value)?;
            }
            if autothrottle_max_delay < delay {
                return Err(PyValueError::new_err(
                    "AutoThrottle maximum delay cannot be smaller than the download delay",
                ));
            }
            if !autothrottle_target_concurrency.is_finite()
                || autothrottle_target_concurrency <= 0.0
            {
                return Err(PyValueError::new_err(
                    "AutoThrottle target concurrency must be a finite positive number",
                ));
            }
        }

        let default_delay = if autothrottle_enabled {
            delay.max(autothrottle_start_delay)
        } else {
            delay
        };
        validate_randomized_duration("randomized download delay", default_delay, randomize_delay)?;
        if autothrottle_enabled {
            validate_randomized_duration(
                "randomized AutoThrottle maximum delay",
                autothrottle_max_delay,
                randomize_delay,
            )?;
        }
        Ok(Self {
            state: Arc::new(Mutex::new(DownloadSlotsState {
                slots: HashMap::new(),
                next_lease_id: 0,
                stats: HashMap::new(),
                pending_stats: HashMap::new(),
                closed: false,
            })),
            notify: Arc::new(Notify::new()),
            default_concurrency: concurrency,
            default_delay,
            randomize_delay,
            autothrottle_enabled,
            min_delay: delay,
            max_delay: autothrottle_max_delay,
            target_concurrency: autothrottle_target_concurrency,
        })
    }

    #[pyo3(signature = (key, concurrency = None, delay = None, randomize_delay = None))]
    fn acquire<'py>(
        &self,
        py: Python<'py>,
        key: String,
        concurrency: Option<usize>,
        delay: Option<f64>,
        randomize_delay: Option<bool>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if key.is_empty() {
            return Err(PyValueError::new_err("download slot key cannot be empty"));
        }
        let concurrency = concurrency.unwrap_or(self.default_concurrency);
        if concurrency == 0 {
            return Err(PyValueError::new_err("slot concurrency must be at least 1"));
        }
        let configured_delay = delay.unwrap_or(self.default_delay);
        validate_duration("slot delay", configured_delay)?;
        let configured_randomize = randomize_delay.unwrap_or(self.randomize_delay);
        validate_randomized_duration(
            "randomized slot delay",
            configured_delay,
            configured_randomize,
        )?;

        let state = self.state.clone();
        let notify = self.notify.clone();
        let default_randomize = self.randomize_delay;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut waiter = SlotWaiter {
                state: state.clone(),
                notify: notify.clone(),
                key: key.clone(),
                registered: false,
            };
            loop {
                let notified = notify.notified();
                let mut wait_for = None;
                {
                    let mut current = state.lock().map_err(|_| {
                        PyRuntimeError::new_err("native download slot state was poisoned")
                    })?;
                    if current.closed {
                        return Err(PyRuntimeError::new_err(
                            "native download slot manager is closed",
                        ));
                    }
                    let now = Instant::now();
                    current.prune_inactive(now, Duration::from_secs(60));
                    let slot = current.slots.entry(key.clone()).or_insert_with(|| {
                        DownloadSlot::new(
                            concurrency,
                            configured_delay,
                            randomize_delay.unwrap_or(default_randomize),
                        )
                    });
                    if !waiter.registered {
                        slot.waiting += 1;
                        waiter.registered = true;
                    }
                    if slot.active.len() < slot.concurrency {
                        let required_delay =
                            Duration::try_from_secs_f64(slot.next_delay).map_err(|_| {
                                PyRuntimeError::new_err("download slot delay cannot be represented")
                            })?;
                        let remaining = slot
                            .last_start
                            .map(|last| required_delay.saturating_sub(now.duration_since(last)))
                            .unwrap_or_default();
                        if remaining.is_zero() {
                            let lease_id = current.next_lease_id;
                            current.next_lease_id =
                                current.next_lease_id.checked_add(1).ok_or_else(|| {
                                    PyOverflowError::new_err(
                                        "download slot lease identifier exhausted",
                                    )
                                })?;
                            let slot = current.slots.get_mut(&key).expect("slot must exist");
                            slot.waiting -= 1;
                            waiter.registered = false;
                            slot.active.insert(lease_id);
                            slot.last_start = Some(now);
                            slot.last_activity = now;
                            slot.next_delay = slot.sample_delay();
                            current.increment("downloader/slot/acquired");
                            return Ok(NativeDownloadSlotLease {
                                state: state.clone(),
                                notify: notify.clone(),
                                key,
                                lease_id,
                                released: false,
                            });
                        }
                        wait_for = Some(remaining);
                    }
                }
                if let Some(duration) = wait_for {
                    tokio::select! {
                        () = tokio::time::sleep(duration) => {}
                        () = notified => {}
                    }
                } else {
                    notified.await;
                }
            }
        })
    }

    #[pyo3(signature = (
        lease,
        latency = None,
        status = None,
        adjust_delay = true
    ))]
    fn release(
        &self,
        mut lease: PyRefMut<'_, NativeDownloadSlotLease>,
        latency: Option<f64>,
        status: Option<u16>,
        adjust_delay: bool,
    ) -> PyResult<()> {
        if let Some(value) = latency
            && (!value.is_finite() || value < 0.0)
        {
            return Err(PyValueError::new_err(
                "download latency must be a finite non-negative number",
            ));
        }
        if !Arc::ptr_eq(&self.state, &lease.state) {
            return Err(PyValueError::new_err(
                "download slot lease belongs to a different manager",
            ));
        }
        if lease.released {
            return Err(PyValueError::new_err(
                "download slot lease was already released",
            ));
        }
        let key = lease.key.clone();
        let lease_id = lease.lease_id;

        {
            let mut state = self.lock_state()?;
            let slot = state
                .slots
                .get_mut(&key)
                .ok_or_else(|| PyValueError::new_err(format!("unknown download slot {key:?}")))?;
            if !slot.active.remove(&lease_id) {
                return Err(PyValueError::new_err(format!(
                    "lease {lease_id} is not active in download slot {key:?}"
                )));
            }
            slot.last_activity = Instant::now();

            let mut stat = "downloader/slot/released";
            if let Some(latency) = latency {
                slot.last_latency = Some(latency);
                if self.autothrottle_enabled && adjust_delay {
                    let target_delay = latency / self.target_concurrency;
                    let averaged_delay = (slot.delay + target_delay) / 2.0;
                    let new_delay = target_delay
                        .max(averaged_delay)
                        .clamp(self.min_delay, self.max_delay);
                    if status != Some(200) && new_delay <= slot.delay {
                        stat = "autothrottle/ignored";
                    } else {
                        stat = if new_delay > slot.delay {
                            "autothrottle/increased"
                        } else if new_delay < slot.delay {
                            "autothrottle/decreased"
                        } else {
                            "autothrottle/unchanged"
                        };
                        slot.delay = new_delay;
                        slot.next_delay = slot.sample_delay();
                    }
                }
            }
            state.increment("downloader/slot/released");
            if stat != "downloader/slot/released" {
                state.increment(stat);
            }
            state.prune_inactive(Instant::now(), Duration::from_secs(60));
        }
        lease.released = true;
        self.notify.notify_waiters();
        Ok(())
    }

    fn close(&self) -> PyResult<()> {
        let mut state = self.lock_state()?;
        state.closed = true;
        state.slots.clear();
        drop(state);
        self.notify.notify_waiters();
        Ok(())
    }

    #[pyo3(signature = (max_idle_seconds = 60.0))]
    fn prune_inactive(&self, max_idle_seconds: f64) -> PyResult<usize> {
        validate_duration("maximum idle time", max_idle_seconds)?;
        let mut state = self.lock_state()?;
        let before = state.slots.len();
        state.prune_inactive(Instant::now(), Duration::from_secs_f64(max_idle_seconds));
        Ok(before - state.slots.len())
    }

    fn drain_stats(&self) -> PyResult<HashMap<String, u64>> {
        Ok(std::mem::take(&mut self.lock_state()?.pending_stats))
    }

    fn stats(&self) -> PyResult<HashMap<String, u64>> {
        Ok(self.lock_state()?.stats.clone())
    }

    fn slot_state(&self, key: &str) -> PyResult<(usize, usize, f64, Option<f64>)> {
        let state = self.lock_state()?;
        let slot = state
            .slots
            .get(key)
            .ok_or_else(|| PyValueError::new_err(format!("unknown download slot {key:?}")))?;
        Ok((
            slot.concurrency,
            slot.active.len(),
            slot.delay,
            slot.last_latency,
        ))
    }

    fn waiting_count(&self, key: &str) -> PyResult<usize> {
        let state = self.lock_state()?;
        let slot = state
            .slots
            .get(key)
            .ok_or_else(|| PyValueError::new_err(format!("unknown download slot {key:?}")))?;
        Ok(slot.waiting)
    }

    #[getter]
    fn slot_count(&self) -> PyResult<usize> {
        Ok(self.lock_state()?.slots.len())
    }
}
