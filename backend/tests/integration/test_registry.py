import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, Source
from app.sources.registry import seed_sources_from_yaml


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_seed_is_idempotent(session, tmp_path):
    yaml_text = (
        "- name: Phoronix\n"
        "  type: rss\n"
        "  url: https://x/feed\n"
        "  main_category: OS性能发展\n"
    )
    f = tmp_path / "seed.yaml"
    f.write_text(yaml_text, encoding="utf-8")
    seed_sources_from_yaml(session, str(f))
    seed_sources_from_yaml(session, str(f))
    count = len(session.scalars(select(Source)).all())
    assert count == 1
