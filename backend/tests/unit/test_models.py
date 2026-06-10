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
