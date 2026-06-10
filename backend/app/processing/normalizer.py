from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.schemas import ExtractedDoc, NormalizedItem, RawItem

_TRACKING_PREFIXES = ("utm_", "ref", "fbclid", "gclid")


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not any(k.lower().startswith(prefix) for prefix in _TRACKING_PREFIXES)
    ]
    query = urlencode(query_pairs)
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize(raw: RawItem, doc: ExtractedDoc) -> NormalizedItem:
    return NormalizedItem(
        source_id=raw.source_id,
        title=(doc.title or raw.title).strip(),
        url=raw.url,
        canonical_url=canonicalize_url(raw.url),
        clean_content=doc.clean_content.strip(),
        published_at=doc.published_at or raw.published_at,
    )
