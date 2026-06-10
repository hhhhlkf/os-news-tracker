from app.processing.normalizer import canonicalize_url, normalize
from app.schemas import ExtractedDoc, RawItem


def test_canonicalize_strips_tracking_and_trailing_slash():
    u = "https://Example.com/Path/?utm_source=x&id=5#frag"
    assert canonicalize_url(u) == "https://example.com/Path?id=5"


def test_canonicalize_removes_trailing_slash():
    assert canonicalize_url("https://x.com/a/") == "https://x.com/a"


def test_normalize_builds_normalized_item():
    raw = RawItem(source_id=1, title="  Hello  ", url="https://x.com/a/?utm_medium=rss")
    doc = ExtractedDoc(url="https://x.com/a", clean_content="body text")
    n = normalize(raw, doc)
    assert n.title == "Hello"
    assert n.canonical_url == "https://x.com/a"
    assert n.clean_content == "body text"
