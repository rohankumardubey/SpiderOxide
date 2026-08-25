use std::collections::HashMap;
use std::str::FromStr;

use num_bigint::BigInt;
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;

#[pyclass(frozen, module = "spideroxide._native")]
pub(crate) struct NativeDepthDecision {
    accepted: bool,
    depth: String,
    priority: String,
}

#[pymethods]
impl NativeDepthDecision {
    #[getter]
    fn accepted(&self) -> bool {
        self.accepted
    }

    #[getter]
    fn depth(&self) -> &str {
        &self.depth
    }

    #[getter]
    fn priority(&self) -> &str {
        &self.priority
    }
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeDepthPolicy {
    max_depth: BigInt,
    priority_adjust: BigInt,
    verbose_stats: bool,
    stats: HashMap<String, u64>,
    pending_stats: HashMap<String, u64>,
    max_depth_seen: Option<BigInt>,
    pending_max_depth: Option<BigInt>,
}

impl NativeDepthPolicy {
    fn parse_integer(value: &str, name: &str) -> PyResult<BigInt> {
        BigInt::from_str(value.trim())
            .map_err(|_| PyValueError::new_err(format!("{name} must be an integer")))
    }

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

    fn record_max_depth(&mut self, depth: &BigInt) {
        let is_new_max = self
            .max_depth_seen
            .as_ref()
            .is_none_or(|current| depth > current);
        if is_new_max {
            self.max_depth_seen = Some(depth.clone());
            self.pending_max_depth = Some(depth.clone());
        }
    }
}

#[pymethods]
impl NativeDepthPolicy {
    #[new]
    #[pyo3(signature = (max_depth = "0", priority_adjust = "0", verbose_stats = false))]
    fn new(max_depth: &str, priority_adjust: &str, verbose_stats: bool) -> PyResult<Self> {
        Ok(Self {
            max_depth: Self::parse_integer(max_depth, "maximum depth")?,
            priority_adjust: Self::parse_integer(priority_adjust, "depth priority")?,
            verbose_stats,
            stats: HashMap::new(),
            pending_stats: HashMap::new(),
            max_depth_seen: None,
            pending_max_depth: None,
        })
    }

    fn record_initial(&mut self) -> PyResult<()> {
        if self.verbose_stats {
            self.increment("request_depth_count/0".to_owned())?;
        }
        Ok(())
    }

    fn process(
        &mut self,
        current_depth: &str,
        current_priority: &str,
    ) -> PyResult<NativeDepthDecision> {
        let depth = Self::parse_integer(current_depth, "current depth")? + BigInt::from(1_u8);
        let priority = Self::parse_integer(current_priority, "current priority")?
            - (&depth * &self.priority_adjust);
        let accepted = self.max_depth == BigInt::from(0_u8) || depth <= self.max_depth;

        if accepted {
            if self.verbose_stats {
                self.increment(format!("request_depth_count/{depth}"))?;
            }
            self.record_max_depth(&depth);
        }

        Ok(NativeDepthDecision {
            accepted,
            depth: depth.to_string(),
            priority: priority.to_string(),
        })
    }

    fn snapshot_counts(&self) -> HashMap<String, u64> {
        self.stats.clone()
    }

    fn drain_counts(&mut self) -> HashMap<String, u64> {
        std::mem::take(&mut self.pending_stats)
    }

    fn max_depth_seen(&self) -> Option<String> {
        self.max_depth_seen.as_ref().map(ToString::to_string)
    }

    fn drain_max_depth(&mut self) -> Option<String> {
        self.pending_max_depth.take().map(|value| value.to_string())
    }

    #[getter]
    fn backend_name(&self) -> &'static str {
        "rust"
    }
}

#[cfg(test)]
mod tests {
    use super::NativeDepthPolicy;

    #[test]
    fn enforces_limits_priorities_and_stats() {
        let mut policy =
            NativeDepthPolicy::new("2", "3", true).expect("depth policy should be valid");
        policy
            .record_initial()
            .expect("initial depth should be recorded");

        let first = policy
            .process("0", "100")
            .expect("first depth should be processed");
        assert!(first.accepted);
        assert_eq!(first.depth, "1");
        assert_eq!(first.priority, "97");

        let second = policy
            .process("1", "97")
            .expect("second depth should be processed");
        assert!(second.accepted);
        assert_eq!(second.depth, "2");
        assert_eq!(second.priority, "91");

        let filtered = policy
            .process("2", "91")
            .expect("filtered depth should still produce a decision");
        assert!(!filtered.accepted);
        assert_eq!(filtered.depth, "3");
        assert_eq!(filtered.priority, "82");

        assert_eq!(policy.stats["request_depth_count/0"], 1);
        assert_eq!(policy.stats["request_depth_count/1"], 1);
        assert_eq!(policy.stats["request_depth_count/2"], 1);
        assert_eq!(policy.max_depth_seen, Some(2.into()));
    }

    #[test]
    fn preserves_arbitrary_size_integers() {
        let huge = "10000000000000000000000000000000000000000";
        let mut policy =
            NativeDepthPolicy::new("0", huge, false).expect("depth policy should be valid");
        let decision = policy
            .process(huge, huge)
            .expect("large integers should be processed");
        assert!(decision.accepted);
        assert_eq!(decision.depth, "10000000000000000000000000000000000000001");
        assert!(decision.priority.starts_with('-'));
        assert_eq!(policy.max_depth_seen, Some(decision.depth.parse().unwrap()));
    }
}
