from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Base, Source, Item, Tag, Entity
from app.enums import SourceType, ItemStatus


def test_models_create_and_relate():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        src = Source(name="Phoronix", type=SourceType.RSS, url="https://x/feed",
                     main_category="OS性能发展")
        item = Item(source=src, title="t", url="https://x/a", url_hash="h1",
                    content_hash="c1", status=ItemStatus.NEW)
        item.tags.append(Tag(name="kernel", kind="sub_tag"))
        item.entities.append(Entity(type="os", name="Linux"))
        s.add(item)
        s.commit()
        assert item.id is not None
        assert item.source.name == "Phoronix"
        assert item.tags[0].name == "kernel"
        assert item.entities[0].name == "Linux"


def test_crawl_method_table_created():
    from app.models import CrawlMethod, CrawlMethodDomain, SiteDiscoveryRun
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        src = Source(name="x.com", type=SourceType.DISCOVERY, url="https://x.com", main_category="OS跟踪来源")
        s.add(src); s.flush()
        m = CrawlMethod(domain="x.com", entry_url="https://x.com", source_id=src.id,
                        dsl_recipe={"actions": []}, signature="abc")
        s.add(m); s.commit()
        assert m.id is not None
        assert m.status == "active"
        assert m.source_id == src.id
        # 去重映射 + 审计表也能建
        s.add(CrawlMethodDomain(domain="x.com", method_id=m.id))
        s.add(SiteDiscoveryRun(site_url="https://x.com", status="running"))
        s.commit()
