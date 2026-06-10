from datetime import datetime
from app.schemas import RawItem, ExtractedDoc, EnrichedFields, EntityRef
from app.enums import InfoType, Importance


def test_raw_item_minimal():
    r = RawItem(source_id=1, title="t", url="https://x/a")
    assert r.raw_content is None


def test_enriched_fields_validation():
    e = EnrichedFields(
        title_tldr="x",
        summary="s",
        key_points=["a", "b"],
        info_type=InfoType.RELEASE,
        importance=Importance.HIGH,
        why_it_matters="w",
        main_category="OS性能发展",
        sub_tags=["kernel"],
        entities=[EntityRef(type="os", name="Linux")],
        confidence=0.9,
    )
    assert e.info_type == InfoType.RELEASE
    assert e.entities[0].name == "Linux"
