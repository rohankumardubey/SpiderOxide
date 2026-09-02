use std::collections::{HashMap, HashSet};
use std::str::FromStr;

use num_bigint::BigInt;
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;

#[pyclass(frozen, module = "spideroxide._native")]
pub(crate) struct NativeRetryDecision {
    retry_times: String,
    priority_adjust: String,
}

#[pymethods]
impl NativeRetryDecision {
    #[getter]
    fn retry_times(&self) -> &str {
        &self.retry_times
    }

    #[getter]
    fn priority_adjust(&self) -> &str {
        &self.priority_adjust
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativePolicyRuntime {
    retry_http_codes: HashSet<i64>,
    stats: HashMap<String, u64>,
    pending_stats: HashMap<String, u64>,
}

impl NativePolicyRuntime {
    fn increment(&mut self, key: String) -> PyResult<()> {
        let value = self.stats.entry(key.clone()).or_default();
        *value = value
            .checked_add(1)
            .ok_or_else(|| PyOverflowError::new_err(format!("stat {key:?} overflowed")))?;
        let pending = self.pending_stats.entry(key.clone()).or_default();
        *pending = pending
            .checked_add(1)
            .ok_or_else(|| PyOverflowError::new_err(format!("pending stat {key:?} overflowed")))?;
        Ok(())
    }

    fn parse_integer(value: &str, name: &str) -> PyResult<BigInt> {
        BigInt::from_str(value.trim())
            .map_err(|_| PyValueError::new_err(format!("{name} must be an integer")))
    }
}

#[pymethods]
impl NativePolicyRuntime {
    #[new]
    #[pyo3(signature = (retry_http_codes = Vec::new()))]
    fn new(retry_http_codes: Vec<i64>) -> Self {
        Self {
            retry_http_codes: retry_http_codes.into_iter().collect(),
            stats: HashMap::new(),
            pending_stats: HashMap::new(),
        }
    }

    #[pyo3(signature = (status, retry_http_codes = None))]
    fn should_retry_status(&self, status: i64, retry_http_codes: Option<Vec<i64>>) -> bool {
        retry_http_codes
            .map(|codes| codes.contains(&status))
            .unwrap_or_else(|| self.retry_http_codes.contains(&status))
    }

    fn should_retry_exception(&self, python_type_match: bool) -> bool {
        python_type_match
    }

    fn can_retry(&self, current_retry_times: &str, max_retry_times: &str) -> PyResult<bool> {
        let current_retry_times = Self::parse_integer(current_retry_times, "current retry times")?;
        let max_retry_times = Self::parse_integer(max_retry_times, "max retry times")?;
        if max_retry_times < BigInt::from(0_u8) {
            return Err(PyValueError::new_err("max retry times cannot be negative"));
        }
        Ok(current_retry_times + BigInt::from(1_u8) <= max_retry_times)
    }

    fn record_request(&mut self, method: &str) -> PyResult<()> {
        self.increment("downloader/request_count".to_owned())?;
        self.increment(format!(
            "downloader/request_method_count/{}",
            method.to_uppercase()
        ))
    }

    fn record_response(&mut self, status: i64) -> PyResult<()> {
        self.increment("downloader/response_count".to_owned())?;
        self.increment(format!("downloader/response_status_count/{status}"))
    }

    fn record_exception(&mut self, exception_type: &str) -> PyResult<()> {
        self.increment("downloader/exception_count".to_owned())?;
        self.increment(format!("downloader/exception_type_count/{exception_type}"))
    }

    fn retry(
        &mut self,
        current_retry_times: &str,
        max_retry_times: &str,
        priority_adjust: &str,
        reason: &str,
        stats_base_key: &str,
    ) -> PyResult<Option<NativeRetryDecision>> {
        let current_retry_times = Self::parse_integer(current_retry_times, "current retry times")?;
        let max_retry_times = Self::parse_integer(max_retry_times, "max retry times")?;
        if max_retry_times < BigInt::from(0_u8) {
            return Err(PyValueError::new_err("max retry times cannot be negative"));
        }
        let retry_times = current_retry_times + BigInt::from(1_u8);
        if retry_times <= max_retry_times {
            let priority_adjust = Self::parse_integer(priority_adjust, "priority adjust")?;
            self.increment(format!("{stats_base_key}/count"))?;
            self.increment(format!("{stats_base_key}/reason_count/{reason}"))?;
            return Ok(Some(NativeRetryDecision {
                retry_times: retry_times.to_string(),
                priority_adjust: priority_adjust.to_string(),
            }));
        }
        self.increment(format!("{stats_base_key}/max_reached"))?;
        Ok(None)
    }

    fn snapshot_stats(&self) -> HashMap<String, u64> {
        self.stats.clone()
    }

    fn drain_stats(&mut self) -> HashMap<String, u64> {
        std::mem::take(&mut self.pending_stats)
    }

    #[getter]
    fn backend_name(&self) -> &'static str {
        "rust"
    }
}

#[cfg(test)]
mod tests {
    use super::NativePolicyRuntime;

    #[test]
    fn retry_decisions_preserve_arbitrary_integer_adjustments() {
        let mut runtime = NativePolicyRuntime::new(vec![503]);
        assert!(runtime.should_retry_status(503, None));
        assert!(runtime.should_retry_exception(true));

        let adjustment = "10000000000000000000000000000000000000000";
        let decision = runtime
            .retry("0", "1", adjustment, "503 Service Unavailable", "retry")
            .expect("retry decision should succeed")
            .expect("first failure should be retried");
        assert_eq!(decision.retry_times, "1");
        assert_eq!(decision.priority_adjust, adjustment);
        assert!(
            runtime
                .retry("1", "1", adjustment, "503 Service Unavailable", "retry")
                .expect("exhaustion decision should succeed")
                .is_none()
        );
        assert_eq!(runtime.stats["retry/count"], 1);
        assert_eq!(runtime.stats["retry/max_reached"], 1);
    }
}
