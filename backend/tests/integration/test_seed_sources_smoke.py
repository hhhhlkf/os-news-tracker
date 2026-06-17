"""
Live seed source smoke probe.

This test intentionally makes real network calls and may call the configured
LLM gateway. It is skipped by default so normal pytest/CI stays deterministic.

Usage:

    RUN_LIVE_SOURCE_PROBE=1 ENABLE_SCHEDULER=0 python -m pytest \
      tests/integration/test_seed_sources_smoke.py -m "live and slow" -v -s
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, Source
from app.sources.registry import seed_sources_from_yaml
from app.sources.seed_source_probe import (
    ProbeStatus,
    probe_source_live,
    result_to_dict,
    summarize_probe_results_with_llm,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture
def seeded_session(session):
    seed_path = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "sources"
        / "seed_sources.yaml"
    )
    result = seed_sources_from_yaml(session, str(seed_path))
    assert result["added"] > 0
    return session


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_SOURCE_PROBE") != "1",
    reason="set RUN_LIVE_SOURCE_PROBE=1 to run live seed source probe",
)
def test_seed_sources_live_probe_report(seeded_session):
    sources = list(
        seeded_session.scalars(
            select(Source).order_by(Source.main_category, Source.name)
        )
    )
    assert sources, "seed source config loaded zero sources"

    print(
        "\nSeed source live probe "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    print(f"total_sources={len(sources)}")

    results = []
    for source in sources:
        result = probe_source_live(source)
        results.append(result)
        seeded_session.rollback()
        print(json.dumps(result_to_dict(result), ensure_ascii=False, sort_keys=True))

    stats = Counter(result.status for result in results)
    aggregate = {
        "total_sources": len(results),
        "ok": stats[ProbeStatus.OK],
        "empty": stats[ProbeStatus.EMPTY],
        "failed": stats[ProbeStatus.FAILED],
        "adapter_unimplemented": stats[ProbeStatus.UNIMPLEMENTED],
        "total_items": sum(result.items_count for result in results),
        "validation_errors": dict(
            sum((Counter(result.validation_errors) for result in results), Counter())
        ),
    }
    print("GLOBAL_STATS " + json.dumps(aggregate, ensure_ascii=False, sort_keys=True))

    try:
        summary = summarize_probe_results_with_llm(results)
    except Exception as exc:
        summary = f"LLM_SUMMARY_FAILED {type(exc).__name__}: {exc}"
    print("LLM_SUMMARY\n" + summary)

    assert len(results) == len(sources)
