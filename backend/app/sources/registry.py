import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Source


def seed_sources_from_yaml(session: Session, path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        entries = yaml.safe_load(fh) or []

    added = 0
    for entry in entries:
        existing = session.scalar(select(Source).where(Source.name == entry["name"]))
        if existing:
            continue
        session.add(
            Source(
                name=entry["name"],
                type=entry["type"],
                url=entry.get("url", ""),
                keywords=entry.get("keywords"),
                adapter=entry.get("adapter"),
                stream=entry.get("stream", "news"),
                vendor=entry.get("vendor"),
                fetch_cron=entry.get("fetch_cron"),
                main_category=entry.get("main_category"),
                relevance_filter=entry.get("relevance_filter", False),
                relevance_keywords=entry.get("relevance_keywords"),
            )
        )
        added += 1

    session.commit()
    return added
