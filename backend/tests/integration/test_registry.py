from pathlib import Path

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


def test_seed_reads_api_config(session, tmp_path):
    yaml_text = (
        "- name: JsonApiNews\n"
        "  type: api\n"
        "  url: https://example.com/news.json\n"
        "  adapter: generic_json_list\n"
        "  stream: news\n"
        "  main_category: OS跟踪来源\n"
        "  api_config:\n"
        "    items_path: data.items\n"
        "    fields:\n"
        "      title: title\n"
        "      content: body\n"
    )
    f = tmp_path / "seed_api.yaml"
    f.write_text(yaml_text, encoding="utf-8")

    seed_sources_from_yaml(session, str(f))

    src = session.scalar(select(Source).where(Source.name == "JsonApiNews"))
    assert src is not None
    assert src.api_config == {
        "items_path": "data.items",
        "fields": {"title": "title", "content": "body"},
    }


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


def test_seed_reads_manifest_includes_in_order(session, tmp_path):
    first = tmp_path / "first.yaml"
    first.write_text(
        "- name: FirstSource\n"
        "  type: rss\n"
        "  url: https://example.com/first.xml\n"
        "  main_category: OS跟踪来源\n",
        encoding="utf-8",
    )
    second_dir = tmp_path / "nested"
    second_dir.mkdir()
    second = second_dir / "second.yaml"
    second.write_text(
        "- name: SecondSource\n"
        "  type: page_monitor\n"
        "  url: https://example.com/second\n"
        "  main_category: OS性能发展\n"
        "  link_selector: a.article\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "seed_sources.yaml"
    manifest.write_text(
        "includes:\n"
        "  - first.yaml\n"
        "  - nested/second.yaml\n",
        encoding="utf-8",
    )

    result = seed_sources_from_yaml(session, str(manifest))

    sources = session.scalars(select(Source).order_by(Source.id)).all()
    assert result == {"added": 2, "updated": 0, "deleted": 0}
    assert [source.name for source in sources] == ["FirstSource", "SecondSource"]
    assert sources[1].link_selector == "a.article"


def test_seed_manifest_rejects_duplicate_source_names(session, tmp_path):
    first = tmp_path / "first.yaml"
    first.write_text(
        "- name: DuplicateSource\n"
        "  type: rss\n"
        "  url: https://example.com/first.xml\n"
        "  main_category: OS跟踪来源\n",
        encoding="utf-8",
    )
    second = tmp_path / "second.yaml"
    second.write_text(
        "- name: DuplicateSource\n"
        "  type: rss\n"
        "  url: https://example.com/second.xml\n"
        "  main_category: OS性能发展\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "seed_sources.yaml"
    manifest.write_text(
        "includes:\n"
        "  - first.yaml\n"
        "  - second.yaml\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate source name"):
        seed_sources_from_yaml(session, str(manifest))


def test_project_seed_manifest_loads_all_sources(session):
    seed_path = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "sources"
        / "seed_sources.yaml"
    )

    result = seed_sources_from_yaml(session, str(seed_path))

    sources = session.scalars(select(Source)).all()
    names = {source.name for source in sources}
    assert result == {"added": 78, "updated": 0, "deleted": 0}
    assert len(sources) == 78
    assert "OpenAnolis News" in names
    assert "ANAS Errata" in names
    assert "ANAS CVE" in names
    assert "Ubuntu Packages" in names

    openanolis_news = session.scalar(
        select(Source).where(Source.name == "OpenAnolis News")
    )
    assert openanolis_news is not None
    assert openanolis_news.type == "api"
    assert openanolis_news.adapter == "generic_json_list"
    assert openanolis_news.enabled is True
    assert openanolis_news.api_config["items_path"] == "data.items"

    anas_cve = session.scalar(select(Source).where(Source.name == "ANAS CVE"))
    assert anas_cve is not None
    assert anas_cve.type == "api"
    assert anas_cve.adapter == "generic_json_list"
    assert anas_cve.enabled is True
    assert anas_cve.api_config["items_path"] == "data.data"
