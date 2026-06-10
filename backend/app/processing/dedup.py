import hashlib
import re


def url_hash(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [_normalize_token(token) for token in tokens]


def _normalize_token(token: str) -> str:
    if token in {"bugfix", "bugfixes"}:
        return token.removeprefix("bug")
    return token


def simhash(text: str, bits: int = 64) -> int:
    vector = [0] * bits
    for token in _tokens(text):
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i in range(bits):
        if vector[i] > 0:
            result |= 1 << i
    return result


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(a: int, b: int, threshold: int = 4) -> bool:
    return hamming(a, b) <= threshold
