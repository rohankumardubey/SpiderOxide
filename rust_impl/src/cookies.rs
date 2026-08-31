use cookie::Cookie;
use cookie_store::CookieStore;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::net::IpAddr;
use url::Url;

fn parse_url(url: &str) -> PyResult<Url> {
    let mut parsed = Url::parse(url)
        .map_err(|error| PyValueError::new_err(format!("invalid cookie URL: {error}")))?;
    if let Some(host) = parsed.host_str()
        && host.ends_with('.')
    {
        let normalized = host.trim_end_matches('.').to_owned();
        parsed
            .set_host(Some(&normalized))
            .map_err(|error| PyValueError::new_err(format!("invalid cookie URL: {error}")))?;
    }
    Ok(parsed)
}

fn is_public_suffix(domain: &str) -> bool {
    psl::suffix(domain.as_bytes()).is_some_and(|suffix| suffix.is_known() && suffix == domain)
}

#[pyclass(module = "spideroxide._native", unsendable)]
#[derive(Default)]
pub(crate) struct NativeCookieJar {
    store: CookieStore,
}

#[pymethods]
impl NativeCookieJar {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn add_cookie(&mut self, url: &str, value: &str) -> PyResult<bool> {
        let request_url = parse_url(url)?;
        let Ok(mut cookie) = Cookie::parse(value.to_owned()) else {
            return Ok(false);
        };

        let request_domain = request_url
            .host_str()
            .unwrap_or_default()
            .trim_end_matches('.')
            .to_ascii_lowercase();
        if cookie.domain().is_none()
            && request_domain.contains('.')
            && request_domain.parse::<IpAddr>().is_err()
        {
            // Python's CookieJar sends host cookies to subdomains; retain Scrapy parity.
            cookie.set_domain(request_domain.clone());
        }

        if let Some(domain) = cookie.domain().map(|value| {
            value
                .trim_start_matches('.')
                .trim_end_matches('.')
                .to_ascii_lowercase()
        }) && is_public_suffix(&domain)
        {
            if domain != request_domain {
                return Ok(false);
            }
            cookie.unset_domain();
        }

        Ok(self.store.insert_raw(&cookie, &request_url).is_ok())
    }

    fn cookie_header(&self, url: &str) -> PyResult<Option<String>> {
        let request_url = parse_url(url)?;
        let values = self
            .store
            .get_request_values(&request_url)
            .map(|(name, value)| format!("{name}={value}"))
            .collect::<Vec<_>>();
        Ok((!values.is_empty()).then(|| values.join("; ")))
    }

    fn clear(&mut self) {
        self.store.clear();
    }

    fn __len__(&self) -> usize {
        self.store.iter_unexpired().count()
    }
}
