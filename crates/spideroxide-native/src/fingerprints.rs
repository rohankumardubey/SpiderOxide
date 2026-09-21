use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use sha1::{Digest, Sha1};

pub(crate) type RequestFingerprint = [u8; 20];
pub(crate) type FingerprintHeaders = Vec<(Vec<u8>, Vec<Vec<u8>>)>;

const PATH_SAFE: &[u8] =
    b":/?[]@!$&'()*+,;=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~|%";
const COMPONENT_SAFE: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~";
const FRAGMENT_SAFE: &[u8] =
    b":/?#[]@!$&'()*+,;=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~|%";

fn strip_url(url: &str) -> String {
    url.trim_matches(|character: char| character <= '\u{20}')
        .chars()
        .filter(|character| !matches!(character, '\t' | '\r' | '\n'))
        .collect()
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn push_percent_encoded(output: &mut String, byte: u8) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    output.push('%');
    output.push(char::from(HEX[usize::from(byte >> 4)]));
    output.push(char::from(HEX[usize::from(byte & 0x0f)]));
}

fn quote_bytes(value: &[u8], safe: &[u8], plus_spaces: bool) -> String {
    let mut output = String::with_capacity(value.len());
    for &byte in value {
        if safe.contains(&byte) {
            output.push(char::from(byte));
        } else if plus_spaces && byte == b' ' {
            output.push('+');
        } else {
            push_percent_encoded(&mut output, byte);
        }
    }
    output
}

fn percent_decode(value: &str, protect_path_separators: bool) -> Vec<u8> {
    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%'
            && index + 2 < bytes.len()
            && let (Some(high), Some(low)) =
                (hex_value(bytes[index + 1]), hex_value(bytes[index + 2]))
        {
            let decoded = (high << 4) | low;
            if protect_path_separators && matches!(decoded, b'/' | b'?') {
                output.push(b'%');
                output.push(bytes[index + 1].to_ascii_uppercase());
                output.push(bytes[index + 2].to_ascii_uppercase());
            } else {
                output.push(decoded);
            }
            index += 3;
            continue;
        }
        output.push(bytes[index]);
        index += 1;
    }
    output
}

fn canonicalize_path(path: &str) -> String {
    let decoded = percent_decode(path, true);
    let quoted = quote_bytes(&decoded, PATH_SAFE, false);
    if quoted.is_empty() {
        "/".to_owned()
    } else {
        quoted
    }
}

fn canonicalize_query(query: &str) -> String {
    let mut pairs = Vec::new();
    for ampersand_part in query.split('&') {
        for part in ampersand_part.split(';') {
            if part.is_empty() {
                continue;
            }
            let (name, value) = part.split_once('=').unwrap_or((part, ""));
            pairs.push((
                percent_decode(&name.replace('+', " "), false),
                percent_decode(&value.replace('+', " "), false),
            ));
        }
    }
    pairs.sort();
    pairs
        .into_iter()
        .map(|(name, value)| {
            format!(
                "{}={}",
                quote_bytes(&name, COMPONENT_SAFE, true),
                quote_bytes(&value, COMPONENT_SAFE, true)
            )
        })
        .collect::<Vec<_>>()
        .join("&")
}

fn canonicalize_authority(authority: &str) -> String {
    let normalized = if authority.is_ascii() {
        authority.to_owned()
    } else {
        #[allow(deprecated)]
        idna::Config::default()
            .transitional_processing(true)
            .to_ascii(authority)
            .unwrap_or_else(|_| authority.to_owned())
    };
    let mut parts = normalized.rsplitn(2, '@');
    let host = parts.next().unwrap_or_default().to_ascii_lowercase();
    parts.next().map_or_else(
        || host.trim_end_matches(':').to_owned(),
        |userinfo| format!("{userinfo}@{}", host.trim_end_matches(':')),
    )
}

pub(crate) fn canonicalize_url(url: &str, keep_fragments: bool) -> PyResult<String> {
    let cleaned = strip_url(url);
    let scheme_end = cleaned
        .find(':')
        .ok_or_else(|| PyValueError::new_err("URL must include a scheme"))?;
    let scheme = cleaned[..scheme_end].to_ascii_lowercase();
    if !scheme
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_alphabetic)
        || !scheme
            .bytes()
            .skip(1)
            .all(|byte| byte.is_ascii_alphanumeric() || b"+-.".contains(&byte))
    {
        return Err(PyValueError::new_err("invalid URL scheme"));
    }

    let remainder = &cleaned[scheme_end + 1..];
    let (without_fragment, fragment) = remainder
        .split_once('#')
        .map_or((remainder, None), |(value, fragment)| {
            (value, Some(fragment))
        });
    let (without_query, query) = without_fragment
        .split_once('?')
        .map_or((without_fragment, None), |(value, query)| {
            (value, Some(query))
        });
    let (authority, path) = if let Some(authority_path) = without_query.strip_prefix("//") {
        let end = authority_path.find('/').unwrap_or(authority_path.len());
        (Some(&authority_path[..end]), &authority_path[end..])
    } else {
        (None, without_query)
    };

    let mut output = scheme;
    output.push(':');
    if let Some(authority) = authority {
        output.push_str("//");
        output.push_str(&canonicalize_authority(authority));
    }
    output.push_str(&canonicalize_path(path));
    if let Some(query) = query {
        let query = canonicalize_query(query);
        if !query.is_empty() {
            output.push('?');
            output.push_str(&query);
        }
    }
    if keep_fragments
        && let Some(fragment) = fragment
        && !fragment.is_empty()
    {
        output.push('#');
        output.push_str(&quote_bytes(fragment.as_bytes(), FRAGMENT_SAFE, false));
    }
    Ok(output)
}

fn json_string(value: &str) -> String {
    let mut output = String::with_capacity(value.len() + 2);
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            '\u{00}'..='\u{1f}' => {
                output.push_str(&format!("\\u{:04x}", u32::from(character)));
            }
            character if character.is_ascii() => output.push(character),
            character => {
                let value = u32::from(character);
                if value <= 0xffff {
                    output.push_str(&format!("\\u{value:04x}"));
                } else {
                    let adjusted = value - 0x1_0000;
                    let high = 0xd800 + (adjusted >> 10);
                    let low = 0xdc00 + (adjusted & 0x3ff);
                    output.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
                }
            }
        }
    }
    output.push('"');
    output
}

fn hex_bytes(value: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(value.len() * 2);
    for byte in value {
        output.push(char::from(HEX[usize::from(byte >> 4)]));
        output.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    output
}

pub(crate) fn fingerprint_bytes(
    url: &str,
    method: &str,
    body: &[u8],
    headers: &[(Vec<u8>, Vec<Vec<u8>>)],
    keep_fragments: bool,
    verbatim_url: bool,
) -> PyResult<RequestFingerprint> {
    let normalized_method = method.to_uppercase();
    let canonical_url = if verbatim_url {
        url.to_owned()
    } else {
        canonicalize_url(url, keep_fragments)?
    };
    let mut normalized_headers = headers
        .iter()
        .filter(|(_, values)| !values.is_empty())
        .collect::<Vec<_>>();
    normalized_headers.sort_by(|left, right| left.0.cmp(&right.0));
    let headers_json = normalized_headers
        .into_iter()
        .map(|(name, values)| {
            let values = values
                .iter()
                .map(|value| json_string(&hex_bytes(value)))
                .collect::<Vec<_>>()
                .join(", ");
            format!("{}: [{values}]", json_string(&hex_bytes(name)))
        })
        .collect::<Vec<_>>()
        .join(", ");
    let fingerprint_json = format!(
        "{{\"body\": {}, \"headers\": {{{headers_json}}}, \"method\": {}, \"url\": {}}}",
        json_string(&hex_bytes(body)),
        json_string(&normalized_method),
        json_string(&canonical_url),
    );
    let mut digest = Sha1::new();
    digest.update(fingerprint_json.as_bytes());
    Ok(digest.finalize().into())
}

#[pyfunction]
#[pyo3(signature = (
    url,
    method,
    body,
    headers = Vec::new(),
    keep_fragments = false,
    verbatim_url = false
))]
pub(crate) fn fingerprint<'py>(
    py: Python<'py>,
    url: &str,
    method: &str,
    body: &[u8],
    headers: FingerprintHeaders,
    keep_fragments: bool,
    verbatim_url: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    Ok(PyBytes::new(
        py,
        &fingerprint_bytes(url, method, body, &headers, keep_fragments, verbatim_url)?,
    ))
}

#[pyfunction]
pub(crate) fn fingerprint_batch(
    py: Python<'_>,
    requests: Vec<(String, String, Vec<u8>, i64)>,
) -> PyResult<Vec<Py<PyBytes>>> {
    requests
        .iter()
        .map(|(url, method, body, _)| {
            let value = fingerprint_bytes(url, method, body, &[], false, false)?;
            Ok(PyBytes::new(py, &value).unbind())
        })
        .collect()
}

#[pyfunction(name = "_canonicalize_url")]
#[pyo3(signature = (url, keep_fragments = false))]
pub(crate) fn py_canonicalize_url(url: &str, keep_fragments: bool) -> PyResult<String> {
    canonicalize_url(url, keep_fragments)
}
