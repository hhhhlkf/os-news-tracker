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


def test_seed_reads_list_mode_fields(session, tmp_path):
    """New optional fields (link_selector, title_selector, date_selector, stealth)
    are read from YAML and persisted."""
    yaml_text = (
        "- name: NewsList\n"
        "  type: page_monitor\n"
        "  url: https://example.com/news\n"
        "  main_category: OS跟踪来源\n"
        "  link_selector: a.article-link\n"
        "  title_selector: h3.title\n"
        "  date_selector: .post-date\n"
        "  stealth: true\n"
    )
    f = tmp_path / "seed2.yaml"
    f.write_text(yaml_text, encoding="utf-8")
    seed_sources_from_yaml(session, str(f))
    src = session.scalar(select(Source).where(Source.name == "NewsList"))
    assert src is not None
    assert src.link_selector == "a.article-link"
    assert src.title_selector == "h3.title"
    assert src.date_selector == ".post-date"
    assert src.stealth is True


def test_seed_defaults_stealth_to_false(session, tmp_path):
    """When stealth is not specified, it defaults to False."""
    yaml_text = (
        "- name: NormalSource\n"
        "  type: rss\n"
        "  url: https://example.com/feed\n"
        "  main_category: OS跟踪来源\n"
    )
    f = tmp_path / "seed3.yaml"
    f.write_text(yaml_text, encoding="utf-8")
    seed_sources_from_yaml(session, str(f))
    src = session.scalar(select(Source).where(Source.name == "NormalSource"))
    assert src.stealth is False
    assert src.link_selector is None
    assert src.title_selector is None
    assert src.date_selector is None
