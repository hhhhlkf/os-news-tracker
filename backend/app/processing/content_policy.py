"""Content-quality gates and source policy applied before persistence."""

from dataclasses import dataclass
from urllib.parse import urlparse

from app.enums import SourceType
from app.models import Source
from app.schemas import EnrichedFields, NormalizedItem, RawItem

INTERNAL_AI_CATEGORY = "司内AI工具"
INTERNAL_AI_HOSTS = {"km.woa.com", "iwiki.woa.com"}
MIN_LEGACY_PAGE_CONTENT_LEN = 500
BOT_CHALLENGE_MARKERS = (
    "making sure you're not a bot",
    "确保您不是机器人",
    "anubis",
    "proof-of-work",
    "hashcash",
    "please enable javascript",
    "enable javascript",
    "browser verification",
)


@dataclass(frozen=True)
class PolicyRejection:
    reason: str
    detail: str | None = None


def is_bot_challenge_page(item: NormalizedItem) -> bool:
    text = f"{item.title}\n{item.clean_content}".lower()
    return sum(marker in text for marker in BOT_CHALLENGE_MARKERS) >= 2


def legacy_page_quality_rejection(
    source: Source,
    raw: RawItem,
    item: NormalizedItem,
) -> PolicyRejection | None:
    """Reject legacy whole-page monitor output that cannot represent an article."""
    if not (
        source.type == SourceType.PAGE_MONITOR
        and not source.link_selector
        and raw.raw_content is not None
    ):
        return None
    if len(item.clean_content) < MIN_LEGACY_PAGE_CONTENT_LEN:
        return PolicyRejection(
            "legacy_page_short_content",
            f"len={len(item.clean_content)} < {MIN_LEGACY_PAGE_CONTENT_LEN}",
        )
    if item.published_at is None:
        return PolicyRejection("legacy_page_missing_published_at")
    return None


def constrain_category(source: Source, fields: EnrichedFields) -> EnrichedFields:
    """Limit the internal-AI category to the approved internal knowledge hosts."""
    if fields.main_category != INTERNAL_AI_CATEGORY:
        return fields
    if (urlparse(source.url).hostname or "") in INTERNAL_AI_HOSTS:
        return fields
    return fields.model_copy(update={"main_category": source.main_category or "OS跟踪来源"})
