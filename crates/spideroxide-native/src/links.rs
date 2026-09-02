use std::collections::HashSet;

use pyo3::prelude::*;
use scraper::{ElementRef, Html};

type LinkCandidate = (String, String, bool);

fn normalized_names(values: Vec<String>) -> HashSet<String> {
    values
        .into_iter()
        .map(|value| value.to_ascii_lowercase())
        .collect()
}

fn html5_trim(value: &str) -> &str {
    value.trim_matches([' ', '\t', '\n', '\r', '\u{000c}'])
}

fn link_text(element: &ElementRef<'_>) -> String {
    element.text().collect()
}

#[pyfunction]
pub(crate) fn extract_link_candidates(
    html: &str,
    tags: Vec<String>,
    attrs: Vec<String>,
    deny_tags: Vec<String>,
    deny_attrs: Vec<String>,
    strip: bool,
) -> (Vec<LinkCandidate>, bool) {
    let document = Html::parse_document(html);
    let malformed = !document.errors.is_empty();
    let tags = normalized_names(tags);
    let all_tags = tags.contains("*");
    let denied_tags = normalized_names(deny_tags);
    let denied_attrs = normalized_names(deny_attrs);
    let all_attrs = attrs.iter().any(|value| value == "*");
    let attrs: Vec<String> = attrs
        .into_iter()
        .map(|value| value.to_ascii_lowercase())
        .filter(|value| !denied_attrs.contains(value))
        .collect();
    let mut candidates = Vec::new();

    for node in document.tree.nodes() {
        let Some(element) = ElementRef::wrap(node) else {
            continue;
        };
        let name = element.value().name().to_ascii_lowercase();
        if (!all_tags && !tags.contains(&name)) || denied_tags.contains(&name) {
            continue;
        }
        let text = link_text(&element);
        let nofollow = element.value().attr("rel").is_some_and(|rel| {
            rel.split(|value: char| value.is_ascii_whitespace() || value == ',')
                .any(|value| value.eq_ignore_ascii_case("nofollow"))
        });
        if all_attrs {
            for (attr, value) in element.value().attrs() {
                if denied_attrs.contains(attr) {
                    continue;
                }
                candidates.push((
                    (if strip { html5_trim(value) } else { value }).to_owned(),
                    text.clone(),
                    nofollow,
                ));
            }
            continue;
        }
        for attr in &attrs {
            if let Some(value) = element.value().attr(attr) {
                candidates.push((
                    (if strip { html5_trim(value) } else { value }).to_owned(),
                    text.clone(),
                    nofollow,
                ));
            }
        }
    }
    (candidates, malformed)
}

#[cfg(test)]
mod tests {
    use super::extract_link_candidates;

    #[test]
    fn extracts_candidates_in_document_order() {
        let (links, _) = extract_link_candidates(
            r#"<a href="/one"> One </a><area href="/map" rel="nofollow">Map</area>"#,
            vec!["a".into(), "area".into()],
            vec!["href".into()],
            Vec::new(),
            Vec::new(),
            true,
        );

        assert_eq!(
            links,
            vec![
                ("/one".into(), " One ".into(), false),
                ("/map".into(), "Map".into(), true),
            ]
        );
    }

    #[test]
    fn honors_denied_tags_and_attributes() {
        let (links, _) = extract_link_candidates(
            r#"<a href="/one" data-url="/two">One</a><area href="/map">Map</area>"#,
            vec!["a".into(), "area".into()],
            vec!["href".into(), "data-url".into()],
            vec!["area".into()],
            vec!["href".into()],
            true,
        );

        assert_eq!(links, vec![("/two".into(), "One".into(), false)]);
    }
}
