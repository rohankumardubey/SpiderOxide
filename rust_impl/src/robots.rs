use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use robotstxt::DefaultMatcher;
use tokio::sync::Notify;
use url::Url;

enum RobotsPolicy {
    Fetching(Arc<Notify>),
    Ready(Arc<[u8]>),
    Unavailable,
}

struct RobotsState {
    policies: HashMap<String, RobotsPolicy>,
    stats: HashMap<String, u64>,
    pending_stats: HashMap<String, u64>,
    closed: bool,
}

impl RobotsState {
    fn increment(&mut self, key: &str) {
        *self.stats.entry(key.to_owned()).or_default() += 1;
        *self.pending_stats.entry(key.to_owned()).or_default() += 1;
    }
}

fn request_details(url: &str) -> PyResult<(String, String, String)> {
    let parsed =
        Url::parse(url).map_err(|error| PyValueError::new_err(format!("invalid URL: {error}")))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(PyValueError::new_err(
            "robots policy only supports HTTP and HTTPS URLs",
        ));
    }
    let origin = parsed.origin().ascii_serialization();
    let robots_url = format!("{origin}/robots.txt");
    let normalized_url = parsed.to_string();
    Ok((origin, robots_url, normalized_url))
}

fn user_agent_product(user_agent: &str) -> &str {
    let end = user_agent
        .find(|character: char| {
            !(character.is_ascii_alphabetic() || character == '-' || character == '_')
        })
        .unwrap_or(user_agent.len());
    &user_agent[..end]
}

#[pyclass(module = "spideroxide._native", frozen)]
pub(crate) struct NativeRobotsDecision {
    action: &'static str,
    origin: String,
    robots_url: Option<String>,
}

#[pymethods]
impl NativeRobotsDecision {
    #[getter]
    fn action(&self) -> &str {
        self.action
    }

    #[getter]
    fn origin(&self) -> &str {
        &self.origin
    }

    #[getter]
    fn robots_url(&self) -> Option<&str> {
        self.robots_url.as_deref()
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeRobotsDecision(action={:?}, origin={:?})",
            self.action, self.origin
        )
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeRobotsRuntime {
    state: Arc<Mutex<RobotsState>>,
}

impl NativeRobotsRuntime {
    fn lock_state(&self) -> PyResult<MutexGuard<'_, RobotsState>> {
        self.state
            .lock()
            .map_err(|_| PyRuntimeError::new_err("native robots policy state was poisoned"))
    }
}

#[pymethods]
impl NativeRobotsRuntime {
    #[new]
    fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(RobotsState {
                policies: HashMap::new(),
                stats: HashMap::new(),
                pending_stats: HashMap::new(),
                closed: false,
            })),
        }
    }

    fn check(&self, url: &str, user_agent: &str) -> PyResult<NativeRobotsDecision> {
        let (origin, robots_url, normalized_url) = request_details(url)?;
        let mut state = self.lock_state()?;
        if state.closed {
            return Err(PyRuntimeError::new_err(
                "native robots policy runtime is closed",
            ));
        }

        let ready_body = match state.policies.get(&origin) {
            Some(RobotsPolicy::Ready(body)) => Some(body.clone()),
            _ => None,
        };
        if let Some(body) = ready_body {
            drop(state);
            let body = body.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&body);
            let body = String::from_utf8_lossy(body);
            let mut matcher = DefaultMatcher::default();
            let allowed = matcher.one_agent_allowed_by_robots(
                &body,
                user_agent_product(user_agent),
                &normalized_url,
            );
            let mut state = self.lock_state()?;
            if state.closed {
                return Err(PyRuntimeError::new_err(
                    "native robots policy runtime is closed",
                ));
            }
            state.increment(if allowed {
                "robotstxt/allowed"
            } else {
                "robotstxt/forbidden"
            });
            return Ok(NativeRobotsDecision {
                action: if allowed { "allow" } else { "deny" },
                origin,
                robots_url: None,
            });
        }

        let (action, robots_url) = match state.policies.get(&origin) {
            Some(RobotsPolicy::Fetching(_)) => ("wait", None),
            Some(RobotsPolicy::Unavailable) => {
                state.increment("robotstxt/allowed");
                ("allow", None)
            }
            Some(RobotsPolicy::Ready(_)) => unreachable!("ready policy was handled above"),
            None => {
                state.policies.insert(
                    origin.clone(),
                    RobotsPolicy::Fetching(Arc::new(Notify::new())),
                );
                state.increment("robotstxt/request_count");
                ("fetch", Some(robots_url))
            }
        };

        Ok(NativeRobotsDecision {
            action,
            origin,
            robots_url,
        })
    }

    fn wait<'py>(&self, py: Python<'py>, origin: String) -> PyResult<Bound<'py, PyAny>> {
        let state = self.state.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            loop {
                let notified = {
                    let current = state.lock().map_err(|_| {
                        PyRuntimeError::new_err("native robots policy state was poisoned")
                    })?;
                    if current.closed {
                        return Ok(false);
                    }
                    let notify = match current.policies.get(&origin) {
                        Some(RobotsPolicy::Fetching(notify)) => notify.clone(),
                        _ => return Ok(true),
                    };
                    notify.notified_owned()
                };
                notified.await;
            }
        })
    }

    fn complete(&self, origin: &str, status: u16, body: &[u8]) -> PyResult<()> {
        if !(100..=599).contains(&status) {
            return Err(PyValueError::new_err(
                "robots.txt response status must be between 100 and 599",
            ));
        }
        let notify = {
            let mut state = self.lock_state()?;
            let notify = match state.policies.get(origin) {
                Some(RobotsPolicy::Fetching(notify)) => notify.clone(),
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "origin {origin:?} is not fetching robots.txt"
                    )));
                }
            };
            state
                .policies
                .insert(origin.to_owned(), RobotsPolicy::Ready(Arc::from(body)));
            state.increment("robotstxt/response_count");
            state.increment(&format!("robotstxt/response_status_count/{status}"));
            notify
        };
        notify.notify_waiters();
        Ok(())
    }

    fn fail(&self, origin: &str, exception_type: &str) -> PyResult<()> {
        let notify = {
            let mut state = self.lock_state()?;
            let notify = match state.policies.get(origin) {
                Some(RobotsPolicy::Fetching(notify)) => notify.clone(),
                _ => {
                    return Err(PyValueError::new_err(format!(
                        "origin {origin:?} is not fetching robots.txt"
                    )));
                }
            };
            state
                .policies
                .insert(origin.to_owned(), RobotsPolicy::Unavailable);
            state.increment(&format!("robotstxt/exception_count/{exception_type}"));
            notify
        };
        notify.notify_waiters();
        Ok(())
    }

    fn record_bypass(&self) -> PyResult<()> {
        self.lock_state()?.increment("robotstxt/bypassed");
        Ok(())
    }

    fn drain_stats(&self) -> PyResult<HashMap<String, u64>> {
        Ok(std::mem::take(&mut self.lock_state()?.pending_stats))
    }

    fn stats(&self) -> PyResult<HashMap<String, u64>> {
        Ok(self.lock_state()?.stats.clone())
    }

    fn close(&self) -> PyResult<()> {
        let mut state = self.lock_state()?;
        state.closed = true;
        let notifications = state
            .policies
            .values()
            .filter_map(|policy| match policy {
                RobotsPolicy::Fetching(notify) => Some(notify.clone()),
                _ => None,
            })
            .collect::<Vec<_>>();
        state.policies.clear();
        drop(state);
        for notify in notifications {
            notify.notify_waiters();
        }
        Ok(())
    }

    #[getter]
    fn origin_count(&self) -> PyResult<usize> {
        Ok(self.lock_state()?.policies.len())
    }
}
