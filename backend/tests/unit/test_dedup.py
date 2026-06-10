from app.processing.dedup import content_hash, is_near_duplicate, simhash, url_hash


def test_url_hash_stable_and_distinct():
    assert url_hash("https://x.com/a") == url_hash("https://x.com/a")
    assert url_hash("https://x.com/a") != url_hash("https://x.com/b")


def test_content_hash_stable():
    assert content_hash("hello world") == content_hash("hello world")


def test_simhash_near_duplicate_detected():
    a = "Linux kernel 6.9 released with new scheduler improvements and fixes"
    b = "Linux kernel 6.9 released with new scheduler improvements and bugfixes"
    assert is_near_duplicate(simhash(a), simhash(b), threshold=5)


def test_simhash_distinct_not_duplicate():
    a = "Linux kernel 6.9 released with scheduler improvements"
    b = "Apple announces new MacBook with M5 chip and display upgrade"
    assert not is_near_duplicate(simhash(a), simhash(b), threshold=5)
