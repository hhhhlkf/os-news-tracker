"""Diagnostic: fetch source 230 (openeuler.org) via the real fetcher path and
dump every candidate's published_at + raw date string, so we can see why the
secondary date filter drops them all."""
import os
import json

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@db:5432/osnews_empty_test",
)
os.environ.setdefault("ENABLE_SCHEDULER", "0")

from app.db import SessionLocal
from app.models import Source
from app.extract.scrapling_extractor import ScraplingExtractor
from app.scheduler import build_fetcher
from app.search.base import get_search_provider

session = SessionLocal()
try:
    src = session.get(Source, 230)
    print("=== source ===")
    print("id:", src.id, "name:", src.name, "type:", src.type, "enabled:", src.enabled)
    print("url:", src.url)
    print("api_config:", json.dumps(src.api_config, ensure_ascii=False))
    print()
    fetcher = build_fetcher(src, ScraplingExtractor(use_stealth=src.stealth), get_search_provider())
    cands = fetcher.fetch(src)
    print("=== candidates:", len(cands), "===")
    none_count = 0
    dates = []
    for idx, c in enumerate(cands):
        pa = c.published_at
        if pa is None:
            none_count += 1
        else:
            dates.append(pa)
        print(f"[{idx}] pub={pa!r} title={c.title[:50]!r}")
    print()
    print(f"published_at None: {none_count}/{len(cands)}")
    if dates:
        print("min published_at:", min(dates).isoformat())
        print("max published_at:", max(dates).isoformat())
finally:
    session.close()
