from enum import StrEnum


class SourceType(StrEnum):
    RSS = "rss"
    API = "api"
    PAGE_MONITOR = "page_monitor"
    SEARCH = "search"


class Stream(StrEnum):
    NEWS = "news"
    STRUCTURED = "structured"


class ItemStatus(StrEnum):
    NEW = "new"
    ENRICHED = "enriched"
    ENRICH_FAILED = "enrich_failed"
    NEEDS_REVIEW = "needs_review"


class InfoType(StrEnum):
    RELEASE = "发布"
    UPDATE = "更新"
    PERFORMANCE = "性能数据"
    ADAPTATION = "适配"
    PAPER = "论文/研究"
    ANALYSIS = "观点/分析"
    OTHER = "其他"


class Importance(StrEnum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


class EntityType(StrEnum):
    VENDOR = "vendor"
    PRODUCT = "product"
    OS = "os"
    PACKAGE = "package"
    VERSION = "version"
    TOPIC = "topic"


class TagKind(StrEnum):
    MAIN_CATEGORY = "main_category"
    SUB_TAG = "sub_tag"


class AdvisorySeverity(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    MODERATE = "moderate"
    LOW = "low"
    UNKNOWN = "unknown"


class CompatibilityKind(StrEnum):
    HARDWARE = "hardware"
    SOFTWARE = "software"
    PACKAGE = "package"
    IMAGE = "image"
    OSV = "osv"


class MissingDatePolicy(StrEnum):
    """Policy for handling items with missing ``published_at`` during time filtering.

    - ``exclude`` — drop items with no date (current behaviour).
    - ``include_as_now`` — treat items with no date as if they were just published,
      counting them in ``matched`` but also tracked separately in
      ``TimeFilterStats.included_without_date``.
    """

    EXCLUDE = "exclude"
    INCLUDE_AS_NOW = "include_as_now"


MAIN_CATEGORIES = [
    "OS跟踪来源",
    "友商产品信息",
    "软件包适配",
    "OS性能发展",
    "司内AI工具",
]
