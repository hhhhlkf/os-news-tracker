from datetime import datetime
from app.schemas import RawItem, ExtractedDoc, EnrichedFields
from app.enums import InfoType, Importance


def test_raw_item_minimal():
    r = RawItem(source_id=1, title="t", url="https://x/a")
    assert r.raw_content is None


def test_enriched_fields_validation():
    e = EnrichedFields(
        title_zh="中文标题",
        summary="s",
        tech_highlights=["[内核] 调度器改进", "[能效] 改善策略"],
        info_type=InfoType.RELEASE,
        importance=Importance.HIGH,
        main_category="OS性能发展",
        sub_tags=["kernel"],
        keywords=["Linux 6.9"],
        merge_suggestions=[
            {
                "child_tag_id": 1,
                "parent_tag_name": "Linux Kernel",
                "reason": "Linux Kernel is more general",
                "confidence": 0.91,
            }
        ],
        confidence=0.9,
    )
    assert e.info_type == InfoType.RELEASE
    assert e.keywords == ["Linux 6.9"]
    assert e.merge_suggestions[0].child_tag_id == 1
    assert e.merge_suggestions[0].parent_tag_name == "Linux Kernel"
