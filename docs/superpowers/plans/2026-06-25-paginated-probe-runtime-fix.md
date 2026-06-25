# 分页式 Probe 运行时修复 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一键 Agent 运行对 `blogByCategoryPage.json?page=1&pageSize=10` 这类分页 API 能自动翻页抓取多页，而非永远只爬第 1 页 10 条。

**Architecture:** 分三处补齐分页链路——(1) `ApiDiscoveryResult` 携带 `pagination` 字段，`discover_api_source` 主流程回填它并把 `api_url` 改成剥离分页参数后的干净 URL；(2) `_try_runtime_discovery` 把 `pagination` 写入缓存 probe；(3) `_build_plan` 复用 cached probe 前做分页一致性校验，识别上一轮旧代码产出的「脏 probe」（URL 有分页参数但无 pagination）并触发重新探测覆盖。

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy, pytest, unittest.mock

**Spec:** `docs/superpowers/specs/2026-06-25-paginated-probe-runtime-fix-design.md`

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `backend/app/sources/api_discovery.py` | `ApiDiscoveryResult` 加 `pagination` 字段；`discover_api_source` 回填 `pagination` + 用干净 URL；新增 `_strip_pagination_params` | Modify |
| `backend/app/fetchers/agent_crawl.py` | `_try_runtime_discovery` 写入 `pagination`；`_build_plan` 加分页一致性校验；新增 `_is_stale_paginated_probe` | Modify |
| `backend/tests/unit/test_api_discovery.py` | `ApiDiscoveryResult.to_dict()` 含 pagination；`discover_api_source` 成功/非分页路径测试 | Modify |
| `backend/tests/unit/test_agent_crawl.py` | `_try_runtime_discovery` 写 pagination；脏 probe 重探；干净 probe 不重探 | Modify |

**测试命令约定：** 所有后端测试用 `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest <path> -v`（项目无系统 `python`，用 `.venv/bin/python`）。

---

### Task 1: `ApiDiscoveryResult` 增加 `pagination` 字段

**Files:**
- Modify: `backend/app/sources/api_discovery.py:104-131`（`ApiDiscoveryResult` dataclass + `to_dict`）
- Test: `backend/tests/unit/test_api_discovery.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/unit/test_api_discovery.py` 的 `TestApiDiscoveryResult` 类中追加测试（该类已存在，在文件末尾的 `test_to_dict_includes_all_fields` 测试之后追加）：

```python
    def test_to_dict_includes_pagination(self):
        result = ApiDiscoveryResult(
            root_url="https://example.com/blog",
            success=True,
            api_url="https://api.example.com/list",
            pagination={"page_param": "page", "size_param": "pageSize", "has_more_path": "data.hasMore"},
        )
        d = result.to_dict()
        assert d["pagination"] == {"page_param": "page", "size_param": "pageSize", "has_more_path": "data.hasMore"}

    def test_to_dict_pagination_defaults_to_none(self):
        result = ApiDiscoveryResult(root_url="https://example.com/blog")
        assert result.to_dict()["pagination"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_api_discovery.py::TestApiDiscoveryResult::test_to_dict_includes_pagination tests/unit/test_api_discovery.py::TestApiDiscoveryResult::test_to_dict_pagination_defaults_to_none -v`
Expected: FAIL with `KeyError: 'pagination'`（`to_dict` 当前不含 `pagination` 键）。

- [ ] **Step 3: Write minimal implementation**

修改 `backend/app/sources/api_discovery.py` 的 `ApiDiscoveryResult`（`:104-131`）。在 `fields` 字段之后新增 `pagination` 字段，并在 `to_dict` 中暴露：

```python
@dataclass
class ApiDiscoveryResult:
    root_url: str
    success: bool = False
    api_url: str | None = None
    method: str = "GET"
    items_path: str | None = None
    fields: dict = field(default_factory=dict)
    pagination: dict | None = None
    name_suggestion: str = ""
    sample_items: list[dict] = field(default_factory=list)
    real_content_count: int = 0
    candidates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "root_url": self.root_url,
            "success": self.success,
            "api_url": self.api_url,
            "method": self.method,
            "items_path": self.items_path,
            "fields": self.fields,
            "pagination": self.pagination,
            "name_suggestion": self.name_suggestion,
            "sample_items": self.sample_items,
            "real_content_count": self.real_content_count,
            "candidates": self.candidates,
            "notes": self.notes,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_api_discovery.py::TestApiDiscoveryResult -v`
Expected: PASS（4 个 TestApiDiscoveryResult 测试全绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/sources/api_discovery.py backend/tests/unit/test_api_discovery.py
git commit -m "feat(api_discovery): ApiDiscoveryResult 携带 pagination 字段"
```

---

### Task 2: 新增 `_strip_pagination_params` 辅助函数

**Files:**
- Modify: `backend/app/sources/api_discovery.py`（在 `_strip_query_param` 附近新增函数）
- Test: `backend/tests/unit/test_api_discovery.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/unit/test_api_discovery.py` 顶部 import 区追加 `_strip_pagination_params`。当前 import 块为：

```python
from app.sources.api_discovery import (
    ApiCandidate,
    ApiDiscoveryResult,
    _build_probe,
    _find_list_arrays,
    _guess_url_template,
    _infer_pagination,
    _infer_url_template_from_anchors,
    _score,
)
```

改为：

```python
from app.sources.api_discovery import (
    ApiCandidate,
    ApiDiscoveryResult,
    _build_probe,
    _find_list_arrays,
    _guess_url_template,
    _infer_pagination,
    _infer_url_template_from_anchors,
    _score,
    _strip_pagination_params,
)
```

在文件末尾（`TestScorePaginationAwareness` 类之后）追加测试类：

```python
class TestStripPaginationParams:
    """测试 _strip_pagination_params 从 URL 剥离分页参数。"""

    def test_strips_page_and_size_params(self):
        probe_pagination = {"page_param": "page", "size_param": "pageSize"}
        url = "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10"
        result = _strip_pagination_params(url, probe_pagination)
        assert "page=" not in result
        assert "pageSize=" not in result
        assert "categoryNo=" in result

    def test_strips_when_only_page_param(self):
        probe_pagination = {"page_param": "currentPage"}
        url = "https://example.com/api/list?currentPage=1&category=all"
        result = _strip_pagination_params(url, probe_pagination)
        assert "currentPage=" not in result
        assert "category=all" in result

    def test_no_pagination_returns_url_unchanged(self):
        url = "https://example.com/api/list?category=all"
        assert _strip_pagination_params(url, None) == url
        assert _strip_pagination_params(url, {}) == url

    def test_url_without_target_params_unchanged(self):
        probe_pagination = {"page_param": "page"}
        url = "https://example.com/api/list?category=all"
        assert _strip_pagination_params(url, probe_pagination) == url
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_api_discovery.py::TestStripPaginationParams -v`
Expected: FAIL with `ImportError: cannot import name '_strip_pagination_params'`。

- [ ] **Step 3: Write minimal implementation**

在 `backend/app/sources/api_discovery.py` 的 `_strip_query_param` 函数（约 `:410`）之后新增：

```python
def _strip_pagination_params(url: str, pagination: dict | None) -> str:
    """从 url 的 query 串中移除分页参数（page_param 和 size_param）。

    用于把探测捕获的「带 page=1&pageSize=10 的原始 URL」清理成
    「去分页参数后的模板 URL」，让运行侧分页引擎逐页注入 page=N。
    """
    if not pagination:
        return url
    for key in ("page_param", "size_param"):
        param = pagination.get(key)
        if param:
            url = _strip_query_param(url, param)
    return url
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_api_discovery.py::TestStripPaginationParams -v`
Expected: PASS（4 个测试全绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/sources/api_discovery.py backend/tests/unit/test_api_discovery.py
git commit -m "feat(api_discovery): 新增 _strip_pagination_params 剥离分页参数"
```

---

### Task 3: `discover_api_source` 主流程回填 `pagination` 并用干净 URL

**Files:**
- Modify: `backend/app/sources/api_discovery.py`（`discover_api_source` 的「Success — use this candidate」分支，约 `:622-628`）
- Test: `backend/tests/unit/test_api_discovery.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/unit/test_api_discovery.py` 顶部 import 区追加 `discover_api_source`。当前 import 块末尾为 `_strip_pagination_params,`，在该行之后追加：

```python
    discover_api_source,
```

完整 import 块变为：

```python
from app.sources.api_discovery import (
    ApiCandidate,
    ApiDiscoveryResult,
    _build_probe,
    _find_list_arrays,
    _guess_url_template,
    _infer_pagination,
    _infer_url_template_from_anchors,
    _score,
    _strip_pagination_params,
    discover_api_source,
)
```

在文件末尾追加测试类。用 `monkeypatch` 替换 `_capture_and_render`（避免真实 Playwright）和 `_self_check_probe`（避免真实 HTTP），让 `discover_api_source` 走成功路径：

```python
class TestDiscoverApiSourcePagination:
    """测试 discover_api_source 成功路径回填 pagination 并用干净 URL。"""

    def _setup_success(self, monkeypatch, *, api_url, payload, expected_pagination):
        """把 _capture_and_render 和 _self_check_probe 桩成返回成功候选。"""
        captured = [{
            "api_url": api_url,
            "method": "GET",
            "post_data": None,
            "status": 200,
            "payload": payload,
        }]
        monkeypatch.setattr(
            "app.sources.api_discovery._capture_and_render",
            lambda url, **kw: (captured, []),
        )

        from app.schemas import RawItem

        def fake_self_check(probe):
            # 自检只需返回一个带 title+url 的条目即可让候选通过
            return [RawItem(source_id=0, title="t", url="https://example.com/a/1", raw_content="", published_at=None)]

        monkeypatch.setattr(
            "app.sources.api_discovery._self_check_probe",
            fake_self_check,
        )
        return expected_pagination

    def test_success_fills_pagination_and_clean_url(self, monkeypatch):
        api_url = "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10"
        payload = {"data": {"items": [{"title": "a", "no": "1"}, {"title": "b", "no": "2"}], "hasMore": True}}
        self._setup_success(monkeypatch, api_url=api_url, payload=payload, expected_pagination=None)

        result = discover_api_source("https://openanolis.cn/blog")

        assert result.success is True
        # pagination 被回填
        assert result.pagination is not None
        assert result.pagination["page_param"] == "page"
        assert result.pagination["size_param"] == "pageSize"
        assert result.pagination["has_more_path"] == "data.hasMore"
        # api_url 是剥离分页参数后的干净 URL
        assert "page=" not in result.api_url
        assert "pageSize=" not in result.api_url
        assert "categoryNo=" in result.api_url

    def test_non_paginated_api_has_none_pagination(self, monkeypatch):
        api_url = "https://api.example.com/list?category=all"
        payload = {"items": [{"title": "a", "url": "https://example.com/a"}, {"title": "b", "url": "https://example.com/b"}]}
        self._setup_success(monkeypatch, api_url=api_url, payload=payload, expected_pagination=None)

        result = discover_api_source("https://example.com/blog")

        assert result.success is True
        assert result.pagination is None
        # 非分页 API，URL 原样保留
        assert result.api_url == "https://api.example.com/list?category=all"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_api_discovery.py::TestDiscoverApiSourcePagination -v`
Expected: FAIL——`result.pagination` 为 None（当前主流程不回填 pagination），`result.api_url` 仍含 `page=1`。

- [ ] **Step 3: Write minimal implementation**

修改 `backend/app/sources/api_discovery.py` 的 `discover_api_source` 中「Success — use this candidate」分支（约 `:622-627`）。当前代码：

```python
        # Success — use this candidate
        result.success = True
        result.api_url = best.api_url
        result.method = best.method
        result.items_path = best.items_path or ""
        result.fields = probe["fields"]
```

改为：

```python
        # Success — use this candidate
        result.success = True
        result.api_url = _strip_pagination_params(best.api_url, probe.get("pagination"))
        result.method = best.method
        result.items_path = best.items_path or ""
        result.fields = probe["fields"]
        result.pagination = probe.get("pagination")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_api_discovery.py::TestDiscoverApiSourcePagination -v`
Expected: PASS（2 个测试全绿）。

- [ ] **Step 5: Run full api_discovery test file to check no regressions**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_api_discovery.py -v`
Expected: PASS（全部测试绿，包括上一轮的 25 个）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/sources/api_discovery.py backend/tests/unit/test_api_discovery.py
git commit -m "feat(api_discovery): discover_api_source 回填 pagination 并用干净 URL"
```

---

### Task 4: `_try_runtime_discovery` 写入 `pagination`

**Files:**
- Modify: `backend/app/fetchers/agent_crawl.py:214-220`（手拼 probe 处）
- Test: `backend/tests/unit/test_agent_crawl.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/unit/test_agent_crawl.py` 顶部 import 区追加 `ApiDiscoveryResult`。当前 import 区有：

```python
from app.agent.schemas import AgentItem, CrawlPlan, PlanUrl, QualifiedPage, RawPage
from app.fetchers.agent_crawl import AgentCrawlFetcher, _to_raw_item
from app.models import AgentSourceConfig as AgentSourceConfigModel
from app.schemas import RawItem
```

在 `from app.schemas import RawItem` 之后追加：

```python
from app.sources.api_discovery import ApiDiscoveryResult
```

在文件末尾追加测试类。`_try_runtime_discovery` 是 `AgentCrawlFetcher` 的私有方法，通过 `fetcher._try_runtime_discovery(source, config)` 直接调用；mock `discover_api_source` 返回带 pagination 的 result；用一个真实 dict 模拟 candidate source 的 `api_config`，断言写入后含 pagination：

```python
class TestTryRuntimeDiscoveryPagination:
    """测试 _try_runtime_discovery 把 pagination 写入缓存 probe。"""

    def _make_source_with_candidate(self, db, *, candidate_api_config):
        """构造一个 agent source，其 api_config 指向 candidate_source_id=2。"""
        from app.enums import Stream

        candidate = MagicMock()
        candidate.id = 2
        candidate.api_config = candidate_api_config
        candidate.url = "https://openanolis.cn/blog"

        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = Stream.NEWS

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get
        db.commit = MagicMock()
        return agent_source, candidate

    def test_writes_pagination_into_cached_probe(self):
        from app.agent.schemas import AgentSourceConfig
        from unittest.mock import patch

        db = MagicMock()
        agent_source, candidate = self._make_source_with_candidate(
            db, candidate_api_config={},
        )
        config = AgentSourceConfig(
            source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
            crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
            crawl_workers=3, quality_workers=2, summary_workers=2,
        )

        discovery_result = ApiDiscoveryResult(
            root_url="https://openanolis.cn/blog",
            success=True,
            api_url="https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
            method="GET",
            items_path="data.items",
            fields={"title": "title", "url_template": "https://openanolis.cn/blog/{no}"},
            pagination={
                "page_param": "page", "size_param": "pageSize", "size": 10,
                "start_page": 1, "max_pages": 5, "has_more_path": "data.hasMore",
            },
            name_suggestion="openanolis.cn",
        )

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
        )
        # _build_plan_from_api 会调 ApiAdapterFetcher 真实抓取，桩掉它只断言 probe 写入
        fetcher._build_plan_from_api = MagicMock(return_value=CrawlPlan(source_id=1, urls=[]))

        with patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            fetcher._try_runtime_discovery(agent_source, config)

        cached_probe = candidate.api_config["probe"]
        assert cached_probe["pagination"] == discovery_result.pagination
        assert cached_probe["url"] == "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo="

    def test_omits_pagination_key_when_result_has_none(self):
        from app.agent.schemas import AgentSourceConfig
        from unittest.mock import patch

        db = MagicMock()
        agent_source, candidate = self._make_source_with_candidate(
            db, candidate_api_config={},
        )
        config = AgentSourceConfig(
            source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
            crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
            crawl_workers=3, quality_workers=2, summary_workers=2,
        )

        discovery_result = ApiDiscoveryResult(
            root_url="https://example.com/blog",
            success=True,
            api_url="https://api.example.com/list",
            method="GET",
            items_path="items",
            fields={"title": "title", "url": "url"},
            pagination=None,
        )

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
        )
        fetcher._build_plan_from_api = MagicMock(return_value=CrawlPlan(source_id=1, urls=[]))

        with patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            fetcher._try_runtime_discovery(agent_source, config)

        cached_probe = candidate.api_config["probe"]
        assert "pagination" not in cached_probe
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py::TestTryRuntimeDiscoveryPagination -v`
Expected: FAIL——`cached_probe["pagination"]` 抛 `KeyError`（当前 `_try_runtime_discovery` 手拼 probe 不含 pagination）。

- [ ] **Step 3: Write minimal implementation**

修改 `backend/app/fetchers/agent_crawl.py` 的 `_try_runtime_discovery`（`:214-220`）。当前代码：

```python
        probe = {
            "mode": "json_list",
            "method": result.method,
            "url": result.api_url,
            "items_path": result.items_path or "",
            "fields": result.fields,
        }
```

改为：

```python
        probe = {
            "mode": "json_list",
            "method": result.method,
            "url": result.api_url,
            "items_path": result.items_path or "",
            "fields": result.fields,
        }
        if result.pagination:
            probe["pagination"] = result.pagination
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py::TestTryRuntimeDiscoveryPagination -v`
Expected: PASS（2 个测试全绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/fetchers/agent_crawl.py backend/tests/unit/test_agent_crawl.py
git commit -m "feat(agent_crawl): _try_runtime_discovery 写入 pagination 到缓存 probe"
```

---

### Task 5: 新增 `_is_stale_paginated_probe` 辅助函数

**Files:**
- Modify: `backend/app/fetchers/agent_crawl.py`（在 `_looks_like_feed_url` 附近新增函数）
- Test: `backend/tests/unit/test_agent_crawl.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/unit/test_agent_crawl.py` 顶部 import 区追加 `_is_stale_paginated_probe`。当前 import 区有：

```python
from app.fetchers.agent_crawl import AgentCrawlFetcher, _to_raw_item
```

改为：

```python
from app.fetchers.agent_crawl import AgentCrawlFetcher, _is_stale_paginated_probe, _to_raw_item
```

在文件末尾追加测试类：

```python
class TestIsStalePaginatedProbe:
    """测试 _is_stale_paginated_probe 识别上一轮旧代码产出的脏 probe。"""

    def test_url_with_page_param_and_no_pagination_is_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
            "items_path": "data.items",
            "fields": {"title": "title"},
        }
        assert _is_stale_paginated_probe(probe) is True

    def test_url_with_page_param_and_pagination_is_not_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
            "items_path": "data.items",
            "fields": {"title": "title"},
            "pagination": {"page_param": "page", "has_more_path": "data.hasMore"},
        }
        assert _is_stale_paginated_probe(probe) is False

    def test_url_without_pagination_params_is_not_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://api.example.com/list?category=all",
            "items_path": "items",
            "fields": {"title": "title"},
        }
        assert _is_stale_paginated_probe(probe) is False

    def test_camel_case_page_param_detected_as_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://example.com/api/list?currentPage=1",
            "items_path": "data.records",
            "fields": {"title": "title"},
        }
        assert _is_stale_paginated_probe(probe) is True

    def test_non_dict_probe_is_not_stale(self):
        assert _is_stale_paginated_probe(None) is False
        assert _is_stale_paginated_probe("not a dict") is False

    def test_probe_without_url_is_not_stale(self):
        probe = {"mode": "json_list", "items_path": "items", "fields": {}}
        assert _is_stale_paginated_probe(probe) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py::TestIsStalePaginatedProbe -v`
Expected: FAIL with `ImportError: cannot import name '_is_stale_paginated_probe'`。

- [ ] **Step 3: Write minimal implementation**

在 `backend/app/fetchers/agent_crawl.py` 的 `_looks_like_feed_url` 函数（约 `:33-41`）之后新增：

```python
_PAGINATION_QUERY_PARAMS = (
    "page", "pageNo", "pageNum", "currentPage", "current", "p", "pageIndex",
    "pageSize", "size", "limit", "per_page", "perPage", "count", "rows",
)


def _is_stale_paginated_probe(probe: object) -> bool:
    """识别上一轮旧代码产出的「脏 probe」：URL 含分页 query 参数但无 pagination 配置。

    这类 probe 是 _build_probe 还不会生成 pagination 时缓存进 DB 的，会导致
    运行侧分页引擎不触发、永远只爬第一页。命中即应触发重新探测覆盖。
    """
    if not isinstance(probe, dict):
        return False
    url = probe.get("url")
    if not isinstance(url, str) or not url:
        return False
    if probe.get("pagination"):
        return False
    query = urlparse(url).query
    if not query:
        return False
    present = set(urllib.parse.parse_qsl(query, keep_blank_values=True))
    return any(param in present for param in _PAGINATION_QUERY_PARAMS)
```

并在 `backend/app/fetchers/agent_crawl.py` 顶部 import 区确认有 `urllib.parse`。当前 import 区（约 `:10-13`）：

```python
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
```

`urlparse` 已在。但需要 `parse_qsl`。把 `from urllib.parse import urlparse` 改为：

```python
import urllib.parse
from urllib.parse import urlparse
```

（保留 `urlparse` 供文件中其他用法使用，新增 `import urllib.parse` 以便用 `urllib.parse.parse_qsl`。）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py::TestIsStalePaginatedProbe -v`
Expected: PASS（6 个测试全绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/fetchers/agent_crawl.py backend/tests/unit/test_agent_crawl.py
git commit -m "feat(agent_crawl): 新增 _is_stale_paginated_probe 识别脏 probe"
```

---

### Task 6: `_build_plan` 加分页一致性校验触发重探

**Files:**
- Modify: `backend/app/fetchers/agent_crawl.py:162-165`（`_build_plan` 复用 probe 的判定）
- Test: `backend/tests/unit/test_agent_crawl.py`

- [ ] **Step 1: Write the failing test**

在 `backend/tests/unit/test_agent_crawl.py` 文件末尾追加测试类。这些测试直接调 `fetcher._build_plan(source, config)`，mock `discover_api_source` 与 `_build_plan_from_api`：

```python
class TestBuildPlanStaleProbeRediscovers:
    """测试 _build_plan 对脏 probe 触发重探、对干净 probe 直接复用。"""

    def _make_fetcher(self, db):
        return AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
        )

    def _config(self):
        from app.agent.schemas import AgentSourceConfig
        return AgentSourceConfig(
            source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
            crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
            crawl_workers=3, quality_workers=2, summary_workers=2,
        )

    def test_stale_probe_triggers_rediscovery(self):
        from unittest.mock import patch
        from app.sources.api_discovery import ApiDiscoveryResult

        db = MagicMock()
        # candidate 持有脏 probe（URL 有 page=1 但无 pagination）
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://openanolis.cn/blog"
        candidate.api_config = {
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
                "items_path": "data.items",
                "fields": {"title": "title", "url_template": "https://openanolis.cn/blog/{no}"},
            }
        }
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        fetcher = self._make_fetcher(db)
        plan_from_api = CrawlPlan(source_id=1, urls=[PlanUrl(url="https://openanolis.cn/blog/1", guessed_topic="t")])
        fetcher._build_plan_from_api = MagicMock(return_value=plan_from_api)
        fetcher._try_runtime_discovery = MagicMock(return_value=plan_from_api)

        discovery_result = ApiDiscoveryResult(
            root_url="https://openanolis.cn/blog",
            success=True,
            api_url="https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
            items_path="data.items",
            fields={"title": "title"},
            pagination={"page_param": "page", "has_more_path": "data.hasMore"},
        )

        with patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result) as mock_discover:
            plan = fetcher._build_plan(agent_source, self._config())

        # 脏 probe 触发了重探（_try_runtime_discovery 被调用，内部会调 discover_api_source）
        assert fetcher._try_runtime_discovery.called
        # _build_plan_from_api 没有被直接用脏 probe 调用
        assert not fetcher._build_plan_from_api.called
        assert plan is plan_from_api

    def test_clean_probe_with_pagination_is_reused_directly(self):
        db = MagicMock()
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://openanolis.cn/blog"
        candidate.api_config = {
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
                "items_path": "data.items",
                "fields": {"title": "title", "url_template": "https://openanolis.cn/blog/{no}"},
                "pagination": {"page_param": "page", "has_more_path": "data.hasMore"},
            }
        }
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        fetcher = self._make_fetcher(db)
        plan_from_api = CrawlPlan(source_id=1, urls=[PlanUrl(url="https://openanolis.cn/blog/1", guessed_topic="t")])
        fetcher._build_plan_from_api = MagicMock(return_value=plan_from_api)
        fetcher._try_runtime_discovery = MagicMock()

        plan = fetcher._build_plan(agent_source, self._config())

        # 干净 probe 直接复用，不重探
        assert fetcher._build_plan_from_api.called
        assert not fetcher._try_runtime_discovery.called
        assert plan is plan_from_api

    def test_non_paginated_probe_is_reused_directly(self):
        db = MagicMock()
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://api.example.com/list"
        candidate.api_config = {
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://api.example.com/list?category=all",
                "items_path": "items",
                "fields": {"title": "title", "url": "url"},
            }
        }
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "Example API"
        agent_source.url = "https://api.example.com/list"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        fetcher = self._make_fetcher(db)
        plan_from_api = CrawlPlan(source_id=1, urls=[PlanUrl(url="https://example.com/a", guessed_topic="t")])
        fetcher._build_plan_from_api = MagicMock(return_value=plan_from_api)
        fetcher._try_runtime_discovery = MagicMock()

        plan = fetcher._build_plan(agent_source, self._config())

        # 无分页参数的 probe 也直接复用，不重探
        assert fetcher._build_plan_from_api.called
        assert not fetcher._try_runtime_discovery.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py::TestBuildPlanStaleProbeRediscovers -v`
Expected: FAIL——`test_stale_probe_triggers_rediscovery` 失败：当前 `_build_plan` 有 probe 即直接 `_build_plan_from_api`，`_try_runtime_discovery` 不会被调用，断言 `fetcher._try_runtime_discovery.called` 为 False。另外两个测试当前应已通过（干净/非分页 probe 本就走 `_build_plan_from_api`），作为回归保护。

- [ ] **Step 3: Write minimal implementation**

修改 `backend/app/fetchers/agent_crawl.py` 的 `_build_plan`（`:162-165`）。当前代码：

```python
        if not probe:
            probe = api_config.get("probe")
        if isinstance(probe, dict) or (source.api_config and isinstance(source.api_config.get("probe"), dict)):
            return self._build_plan_from_api(source, config)
```

改为：

```python
        if not probe:
            probe = api_config.get("probe")
        if isinstance(probe, dict) and _is_stale_paginated_probe(probe):
            append_run_log(
                "plan",
                "检测到分页式 probe 缺失 pagination 配置，触发重新探测",
                source=source.name,
                url=probe.get("url") or source.url,
                level="warning",
            )
            # 丢弃脏 probe，落到下文 _try_runtime_discovery 重探
            probe = None
        elif isinstance(probe, dict) or (
            source.api_config
            and isinstance(source.api_config.get("probe"), dict)
            and not _is_stale_paginated_probe(source.api_config.get("probe"))
        ):
            return self._build_plan_from_api(source, config)
```

> 注意：`append_run_log` 已在文件顶部 import（`from app.run_logs import append_run_log, clear_run_logs`），无需新增 import。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py::TestBuildPlanStaleProbeRediscovers -v`
Expected: PASS（3 个测试全绿）。

- [ ] **Step 5: Run full agent_crawl test file to check no regressions**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py -v`
Expected: PASS（全部测试绿，包括 Task 4、Task 5 新增的和上一轮已有的）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/fetchers/agent_crawl.py backend/tests/unit/test_agent_crawl.py
git commit -m "feat(agent_crawl): _build_plan 对脏 probe 触发重新探测"
```

---

### Task 7: 端到端链路测试 + 全量回归

**Files:**
- Test: `backend/tests/unit/test_agent_crawl.py`

此任务不新增产品代码，只补端到端覆盖并跑全量回归，验证「探测→写 DB→复用→多页抓取」全链路产出超过单页条数。

- [ ] **Step 1: Write the end-to-end test**

在 `backend/tests/unit/test_agent_crawl.py` 文件末尾追加端到端测试。用真实的 `ConfigurableApiProbeAdapter` 路径（通过桩 `ApiAdapterFetcher` 的 requester 返回多页响应），验证 `_try_runtime_discovery` 写入带 pagination 的 probe 后，`_build_plan_from_api` 能拿到多页条目：

```python
class TestPaginatedProbeEndToEnd:
    """端到端：探测带 pagination 的 result → 写入 candidate → 多页抓取超过单页条数。"""

    def test_rediscovery_yields_multi_page_items(self):
        from unittest.mock import patch
        from app.fetchers.api_adapters import ApiAdapterFetcher
        from app.sources.api_discovery import ApiDiscoveryResult

        # 三页响应：page1/2 各 2 条 hasMore=true，page3 1 条 hasMore=false
        def fake_requester(url):
            from urllib.parse import parse_qs, urlparse as _urlparse
            import json as _json
            page = int(parse_qs(_urlparse(url).query).get("page", ["1"])[0])
            if page == 1:
                items = [{"title": "A", "no": "a"}, {"title": "B", "no": "b"}]
                has_more = True
            elif page == 2:
                items = [{"title": "C", "no": "c"}, {"title": "D", "no": "d"}]
                has_more = True
            else:
                items = [{"title": "E", "no": "e"}]
                has_more = False
            return _json.dumps({"data": {"items": items, "hasMore": has_more}})

        # 用真实的 ApiAdapterFetcher（注入 fake_requester）
        real_fetcher = ApiAdapterFetcher(requester=fake_requester)

        db = MagicMock()
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://openanolis.cn/blog"
        candidate.api_config = {}  # 探测前无 probe
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model(max_urls_per_run=10)
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        discovery_result = ApiDiscoveryResult(
            root_url="https://openanolis.cn/blog",
            success=True,
            api_url="https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
            method="GET",
            items_path="data.items",
            fields={"title": "title", "url_template": "https://openanolis.cn/blog/{no}"},
            pagination={
                "page_param": "page", "size_param": "pageSize", "size": 10,
                "start_page": 1, "max_pages": 5, "has_more_path": "data.hasMore",
            },
            name_suggestion="openanolis.cn",
        )

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
        )
        # 让 _build_plan_from_api 用真实 ApiAdapterFetcher（通过 monkeypatch 类属性不可行，
        # 改为直接替换 fetcher 内部对 ApiAdapterFetcher 的引用：在 _build_plan_from_api
        # 里是 `from app.fetchers.api_adapters import ApiAdapterFetcher` 局部 import，
        # 故 patch 该模块的 ApiAdapterFetcher）。
        with patch("app.fetchers.api_adapters.ApiAdapterFetcher", return_value=real_fetcher), \
             patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            plan = fetcher._build_plan(agent_source, _make_config_model(max_urls_per_run=10) and None or None) if False else None
            # 直接走 _build_plan：脏 probe 不存在（candidate 无 probe），会先尝试 runtime discovery
            from app.agent.schemas import AgentSourceConfig
            config = AgentSourceConfig(
                source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
                crawl_depth=1, max_urls_per_run=10, quality_threshold=4,
                crawl_workers=3, quality_workers=2, summary_workers=2,
            )
            plan = fetcher._build_plan(agent_source, config)

        # 三页共 5 条，超过单页 10 条限制中的「单页实际 2 条」
        assert len(plan.urls) == 5
        titles = [pu.guessed_topic for pu in plan.urls]
        assert "A" in titles and "E" in titles
        # candidate 的缓存 probe 现在带 pagination
        cached_probe = candidate.api_config["probe"]
        assert cached_probe["pagination"]["page_param"] == "page"
```

> 说明：此测试用 `patch("app.fetchers.api_adapters.ApiAdapterFetcher", return_value=real_fetcher)` 让 `_build_plan_from_api` 内部 `from app.fetchers.api_adapters import ApiAdapterFetcher` 拿到的是返回 `real_fetcher` 的桩类，`real_fetcher.fetch(source)` 用 `fake_requester` 翻三页。`candidate.api_config` 起始为空 dict，`_build_plan` 取不到 probe → 落 `_try_runtime_discovery` → 探测成功写入带 pagination 的 probe → `_build_plan_from_api` 多页抓取。

- [ ] **Step 2: Run the end-to-end test**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/unit/test_agent_crawl.py::TestPaginatedProbeEndToEnd -v`
Expected: PASS。若 FAIL，先检查 `_build_plan_from_api` 内 `ApiAdapterFetcher` 的 import 形式是否为局部 `from app.fetchers.api_adapters import ApiAdapterFetcher`（见 `agent_crawl.py:254`），patch 目标需与之对应；若 patch 不生效，改为 `patch("app.fetchers.agent_crawl.ApiAdapterFetcher", ...)` 并在 `_build_plan_from_api` 顶部加 `from app.fetchers.api_adapters import ApiAdapterFetcher` 模块级导入（如已是局部 import，patch `app.fetchers.api_adapters.ApiAdapterFetcher` 即可）。

- [ ] **Step 3: Run full backend test suite**

Run: `cd backend && ENABLE_SCHEDULER=0 .venv/bin/python -m pytest tests/ -v 2>&1 | tail -30`
Expected: 全绿。上一轮基线为 349 passed, 1 skipped；本轮新增 Task 1-7 测试后总数应增加约 17 个，全部 PASS，0 FAIL。

- [ ] **Step 4: Commit**

```bash
git add backend/tests/unit/test_agent_crawl.py
git commit -m "test(agent_crawl): 端到端验证分页 probe 多页抓取链路"
```

---

## Self-Review

**1. Spec coverage:**
- Spec §4.1(a) `ApiDiscoveryResult` 加 `pagination` → Task 1 ✅
- Spec §4.1(b) `discover_api_source` 回填 pagination + 干净 URL + `_strip_pagination_params` → Task 2 + Task 3 ✅
- Spec §4.2(a) `_try_runtime_discovery` 写入 pagination → Task 4 ✅
- Spec §4.2(b) `_build_plan` 分页一致性校验 + `_is_stale_paginated_probe` → Task 5 + Task 6 ✅
- Spec §6.1 `test_api_discovery.py` 测试 → Task 1/2/3 ✅
- Spec §6.1 `test_agent_crawl.py` 测试 → Task 4/5/6 ✅
- Spec §6.2 端到端覆盖 → Task 7 ✅
- Spec §6.3 回归 → Task 7 Step 3 ✅

**2. Placeholder scan:** 无 TBD/TODO；每个代码步骤都含完整代码；测试步骤含完整测试代码与断言。

**3. Type consistency:**
- `_strip_pagination_params(url, pagination)` 签名在 Task 2 定义、Task 3 调用，一致 ✅
- `_is_stale_paginated_probe(probe)` 签名在 Task 5 定义、Task 6 调用，一致 ✅
- `ApiDiscoveryResult.pagination` 字段在 Task 1 定义、Task 3/4/6/7 读取，一致 ✅
- `result.pagination` / `probe.get("pagination")` / `cached_probe["pagination"]` 命名统一 ✅
- `_PAGINATION_QUERY_PARAMS` 与 spec §4.2(b) 列出的参数集一致 ✅

无遗漏，计划可执行。
