"""api_discovery 单元测试 — 验证 JSON 列表数组查找、字段映射、probe 生成。"""

import json

import pytest

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


class TestFindListArrays:
    """测试 _find_list_arrays 递归查找 JSON 中的列表数组。"""

    def test_root_array(self):
        payload = [{"title": "a"}, {"title": "b"}]
        results = _find_list_arrays(payload)
        assert len(results) == 1
        assert results[0][0] == ""
        assert len(results[0][1]) == 2

    def test_nested_array(self):
        payload = {"data": {"records": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}}
        results = _find_list_arrays(payload)
        assert len(results) == 1
        assert results[0][0] == "data.records"
        assert len(results[0][1]) == 3

    def test_multiple_arrays_sorted_by_length(self):
        payload = {
            "short": [{"x": 1}, {"x": 2}],
            "long": [{"t": "a"}, {"t": "b"}, {"t": "c"}, {"t": "d"}],
        }
        results = _find_list_arrays(payload)
        assert len(results) == 2
        assert results[0][0] == "long"
        assert len(results[0][1]) == 4

    def test_skips_short_arrays(self):
        payload = {"data": [{"x": 1}]}
        results = _find_list_arrays(payload)
        assert results == []

    def test_skips_non_dict_items(self):
        payload = {"data": ["a", "b", "c"]}
        results = _find_list_arrays(payload)
        assert results == []


class TestScore:
    """测试 _score 评分逻辑。"""

    def test_full_fields_score_higher_than_partial(self):
        full = _score(
            items=[{"title": "a", "url": "u", "published_at": "2026-01-01", "summary": "s"}] * 10,
            fields={"title": "title", "url": "url", "published_at": "published_at", "content": ["summary"]},
            api_url="https://api.example.com/blog/list",
            page_url="https://example.com/blog",
        )
        partial = _score(
            items=[{"title": "a"}] * 10,
            fields={"title": "title"},
            api_url="https://cdn.example.com/track",
            page_url="https://example.com/blog",
        )
        assert full > partial

    def test_same_domain_bonus(self):
        same = _score(
            items=[{"title": "a"}, {"title": "b"}],
            fields={"title": "title"},
            api_url="https://api.example.com/list",
            page_url="https://example.com/blog",
        )
        diff = _score(
            items=[{"title": "a"}, {"title": "b"}],
            fields={"title": "title"},
            api_url="https://api.other.com/list",
            page_url="https://example.com/blog",
        )
        assert same > diff

    def test_article_list_beats_category_list(self):
        """An API with content fields should outscore a metadata-only API,
        even if the metadata API has more items."""
        category_api = _score(
            items=[{"name": "cat1", "no": "1", "gmtCreate": "2026-01-01"}] * 20,
            fields={"title": "name", "published_at": "gmtCreate"},
            api_url="https://example.com/api/blog/getBlogCategory.json",
            page_url="https://example.com/blog",
        )
        article_api = _score(
            items=[{"title": "post1", "summary": "content", "publishTime": "2026-01-01", "no": "1"}] * 10,
            fields={"title": "title", "published_at": "publishTime", "content": ["summary"]},
            api_url="https://example.com/api/blog/blogByCategoryPage.json?categoryNo=&page=1",
            page_url="https://example.com/blog",
        )
        assert article_api > category_api


class TestUrlTemplate:
    """测试 url_template 推断。"""

    def test_infer_from_anchors_matches_id(self):
        items = [{"id": 42, "title": "Post A"}, {"id": 43, "title": "Post B"}]
        anchors = [
            "https://example.com/blog/42",
            "https://example.com/blog/43",
            "https://example.com/about",
        ]
        template = _infer_url_template_from_anchors(items, anchors)
        assert template is not None
        assert "{item.id}" in template

    def test_infer_from_anchors_matches_path_field(self):
        """item 没有 id 字段、但有 path 字段（相对路径）时，应能从锚点反推模板。

        复现 openEuler: API item 只有 ``path='zh/blog/xxx/xxx'``，锚点里
        存在 ``.../xxx.html``，需推出 ``{item.path}`` 模板，而不是要求字段名
        必须在预置的 _ID_LIKE_KEYS 候选里。
        """
        items = [
            {"path": "zh/blog/post-a/post-a", "title": "A", "lang": "zh"},
            {"path": "zh/blog/post-b/post-b", "title": "B", "lang": "zh"},
        ]
        anchors = [
            "https://www.openeuler.org/zh/blog/post-a/post-a.html",
            "https://www.openeuler.org/zh/blog/post-b/post-b.html",
        ]
        template = _infer_url_template_from_anchors(items, anchors)
        assert template is not None
        assert "{item.path}" in template
        # 通用字段 lang（所有 item 值相同）不应被误选为模板占位符
        assert "{item.lang}" not in template

    def test_infer_returns_none_when_no_match(self):
        items = [{"id": 42, "title": "Post A"}]
        anchors = ["https://example.com/about"]
        assert _infer_url_template_from_anchors(items, anchors) is None

    def test_guess_template_from_page_url(self):
        items = [{"slug": "hello-world", "title": "Hello"}]
        template = _guess_url_template(items, "https://example.com/blog")
        assert template is not None
        assert "{item.slug}" in template
        assert "example.com/blog/detail/" in template

    def test_guess_returns_none_without_id_like_field(self):
        items = [{"title": "Hello"}]
        assert _guess_url_template(items, "https://example.com/blog") is None


class TestBuildProbe:
    """测试 _build_probe 生成与 ApiAdapterFetcher 兼容的配置。"""

    def test_probe_with_url_field(self):
        candidate = ApiCandidate(
            api_url="https://api.example.com/list",
            method="GET",
            post_data=None,
            status=200,
            items_path="data.records",
            items=[{"title": "a", "url": "u1"}, {"title": "b", "url": "u2"}],
            fields={"title": "title", "url": "url"},
            score=10,
        )
        notes: list[str] = []
        probe = _build_probe(candidate, [], "https://example.com/blog", notes)

        assert probe["mode"] == "json_list"
        assert probe["method"] == "GET"
        assert probe["url"] == "https://api.example.com/list"
        assert probe["items_path"] == "data.records"
        assert probe["fields"]["title"] == "title"
        assert probe["fields"]["url"] == "url"
        assert "url_template" not in probe["fields"]
        assert notes == []

    def test_probe_infers_url_template_from_anchors(self):
        candidate = ApiCandidate(
            api_url="https://api.example.com/list",
            method="GET",
            post_data=None,
            status=200,
            items_path="",
            items=[{"id": 42, "title": "a"}, {"id": 43, "title": "b"}],
            fields={"title": "title"},
            score=5,
        )
        anchors = ["https://example.com/blog/42", "https://example.com/blog/43"]
        notes: list[str] = []
        probe = _build_probe(candidate, anchors, "https://example.com/blog", notes)

        assert "url_template" in probe["fields"]
        assert "{item.id}" in probe["fields"]["url_template"]
        assert notes == []

    def test_probe_falls_back_to_guessed_template_with_note(self):
        candidate = ApiCandidate(
            api_url="https://api.example.com/list",
            method="GET",
            post_data=None,
            status=200,
            items_path="",
            items=[{"id": 42, "title": "a"}],
            fields={"title": "title"},
            score=5,
        )
        notes: list[str] = []
        probe = _build_probe(candidate, [], "https://example.com/blog", notes)

        assert "url_template" in probe["fields"]
        assert len(notes) == 1
        assert "推测值" in notes[0]

    def test_probe_includes_json_body_for_post(self):
        candidate = ApiCandidate(
            api_url="https://api.example.com/list",
            method="POST",
            post_data=json.dumps({"page": 1, "size": 10}),
            status=200,
            items_path="data.records",
            items=[{"title": "a", "url": "u1"}, {"title": "b", "url": "u2"}],
            fields={"title": "title", "url": "url"},
            score=10,
        )
        notes: list[str] = []
        probe = _build_probe(candidate, [], "https://example.com/blog", notes)

        assert probe["method"] == "POST"
        # page/size 在 POST body 里被识别为分页参数并剥离，交给分页引擎逐页注入；
        # json_body 保留其余非分页字段（此处为空 dict）。
        assert probe["json_body"] == {}
        assert probe["pagination"]["page_param"] == "page"
        assert probe["pagination"]["size_param"] == "size"


class TestApiDiscoveryResult:
    """测试 ApiDiscoveryResult.to_dict()。"""

    def test_to_dict_includes_all_fields(self):
        result = ApiDiscoveryResult(
            root_url="https://example.com/blog",
            success=True,
            api_url="https://api.example.com/list",
            method="GET",
            items_path="data",
            fields={"title": "title", "url": "url"},
            name_suggestion="Example",
            sample_items=[{"title": "T", "url": "U", "published_at": None, "content_preview": ""}],
            real_content_count=1,
            notes=["note"],
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["api_url"] == "https://api.example.com/list"
        assert d["items_path"] == "data"
        assert d["fields"] == {"title": "title", "url": "url"}
        assert d["real_content_count"] == 1
        assert d["notes"] == ["note"]
        assert d["sample_items"][0]["title"] == "T"

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


class TestInferPagination:
    """测试 _infer_pagination 从 api_url + payload 推断分页配置。"""

    def _candidate(
        self,
        api_url: str,
        *,
        items_path: str = "data.items",
        payload: object | None = None,
        method: str = "GET",
        post_data: str | None = None,
    ) -> ApiCandidate:
        return ApiCandidate(
            api_url=api_url,
            method=method,
            post_data=post_data,
            status=200,
            items_path=items_path,
            items=[{"title": "a"}, {"title": "b"}],
            fields={"title": "title"},
            score=10,
            payload=payload,
        )

    def test_infers_page_and_size_params_with_has_more(self):
        candidate = self._candidate(
            "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
            payload={"data": {"items": [], "hasMore": True, "total": 42}},
        )
        notes: list[str] = []
        pagination = _infer_pagination(candidate, notes)

        assert pagination is not None
        assert pagination["page_param"] == "page"
        assert pagination["size_param"] == "pageSize"
        assert pagination["size"] == 10
        assert pagination["start_page"] == 1
        assert pagination["has_more_path"] == "data.hasMore"
        assert pagination["max_pages"] >= 1
        assert notes  # 推断说明被写入 notes

    def test_infers_total_path_when_no_has_more(self):
        candidate = self._candidate(
            "https://example.com/api/articles?page=1&size=5",
            payload={"data": {"records": [], "total": 23}},
        )
        notes: list[str] = []
        pagination = _infer_pagination(candidate, notes)

        assert pagination is not None
        assert pagination["page_param"] == "page"
        assert pagination["size_param"] == "size"
        assert "has_more_path" not in pagination
        assert pagination["total_path"] == "data.total"

    def test_infers_pagination_from_post_json_body(self):
        """分页参数在 POST JSON body 里（而非 URL query）时也应识别。

        复现 openEuler: ``POST /api-search/search/sort/blog``，body 是
        ``{"category":"blog","lang":"zh","page":1,"pageSize":12}``，URL 上
        没有任何 query 参数；payload 用 ``obj.count`` 给出总条数。
        """
        candidate = self._candidate(
            "https://www.openeuler.org/api-search/search/sort/blog",
            method="POST",
            post_data='{"category":"blog","lang":"zh","page":1,"pageSize":12}',
            items_path="obj.records",
            payload={"status": 0, "obj": {"records": [], "count": 358, "pageSize": 12, "page": 1}},
        )
        notes: list[str] = []
        pagination = _infer_pagination(candidate, notes)

        assert pagination is not None
        assert pagination["page_param"] == "page"
        assert pagination["size_param"] == "pageSize"
        assert pagination["size"] == 12
        assert pagination["start_page"] == 1
        assert "has_more_path" not in pagination
        assert pagination["total_path"] == "obj.count"

    def test_returns_none_without_page_param(self):
        candidate = self._candidate(
            "https://example.com/api/articles?category=all",
            payload={"data": {"items": []}},
        )
        notes: list[str] = []
        assert _infer_pagination(candidate, notes) is None

    def test_recognizes_camel_case_page_param(self):
        candidate = self._candidate(
            "https://example.com/api/list?currentPage=1&pageSize=20",
            payload={"data": {"items": [], "hasNext": False}},
        )
        notes: list[str] = []
        pagination = _infer_pagination(candidate, notes)

        assert pagination is not None
        assert pagination["page_param"] == "currentPage"
        assert pagination["size_param"] == "pageSize"
        assert pagination["has_more_path"] == "data.hasNext"


class TestBuildProbePagination:
    """测试 _build_probe 把分页参数从 url 剥离并写入 pagination。"""

    def test_probe_strips_page_params_and_adds_pagination(self):
        candidate = ApiCandidate(
            api_url="https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
            method="GET",
            post_data=None,
            status=200,
            items_path="data.items",
            items=[{"title": "a", "no": "1"}, {"title": "b", "no": "2"}],
            fields={"title": "title"},
            score=10,
            payload={"data": {"items": [], "hasMore": True}},
        )
        notes: list[str] = []
        probe = _build_probe(candidate, [], "https://openanolis.cn/blog", notes)

        # page/pageSize 已从 url 剥离，交给分页引擎控制
        assert "page=" not in probe["url"]
        assert "pageSize=" not in probe["url"]
        assert "categoryNo=" in probe["url"]  # 非分页参数保留
        assert probe["pagination"]["page_param"] == "page"
        assert probe["pagination"]["size_param"] == "pageSize"
        assert probe["pagination"]["has_more_path"] == "data.hasMore"

    def test_probe_without_page_params_has_no_pagination(self):
        candidate = ApiCandidate(
            api_url="https://api.example.com/list",
            method="GET",
            post_data=None,
            status=200,
            items_path="data.records",
            items=[{"title": "a", "url": "u1"}, {"title": "b", "url": "u2"}],
            fields={"title": "title", "url": "url"},
            score=10,
            payload={"data": {"records": []}},
        )
        notes: list[str] = []
        probe = _build_probe(candidate, [], "https://example.com/blog", notes)

        assert "pagination" not in probe
        assert probe["url"] == "https://api.example.com/list"


class TestScorePaginationAwareness:
    """测试 _score 不再把 blogByCategoryPage 这类分页文章列表误判为元数据。"""

    def test_paginated_article_list_outscore_pure_category_metadata(self):
        article = _score(
            items=[{"title": "post", "summary": "s", "no": "1"}] * 10,
            fields={"title": "title", "published_at": "publishTime", "content": ["summary"]},
            api_url="https://example.com/api/blog/blogByCategoryPage.json?categoryNo=&page=1",
            page_url="https://example.com/blog",
        )
        metadata = _score(
            items=[{"name": "cat1", "no": "1"}] * 20,
            fields={"title": "name", "published_at": "gmtCreate"},
            api_url="https://example.com/api/blog/getBlogCategory.json",
            page_url="https://example.com/blog",
        )
        assert article > metadata

    def test_pure_category_endpoint_is_penalized(self):
        """纯分类元数据 API（无 page/list 文章信号）应被扣分。"""
        penalized = _score(
            items=[{"name": "cat"}] * 5,
            fields={"title": "name"},
            api_url="https://example.com/api/blog/categories.json",
            page_url="https://example.com/blog",
        )
        neutral = _score(
            items=[{"title": "post"}] * 5,
            fields={"title": "title"},
            api_url="https://example.com/api/blog/list",
            page_url="https://example.com/blog",
        )
        assert neutral > penalized


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


class TestDiscoverApiSourcePagination:
    """测试 discover_api_source 成功路径回填 pagination 并用干净 URL。"""

    def _setup_success(self, monkeypatch, *, api_url, payload):
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

    def test_success_fills_pagination_and_clean_url(self, monkeypatch):
        api_url = "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10"
        payload = {"data": {"items": [{"title": "a", "no": "1"}, {"title": "b", "no": "2"}], "hasMore": True}}
        self._setup_success(monkeypatch, api_url=api_url, payload=payload)

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
        self._setup_success(monkeypatch, api_url=api_url, payload=payload)

        result = discover_api_source("https://example.com/blog")

        assert result.success is True
        assert result.pagination is None
        # 非分页 API，URL 原样保留
        assert result.api_url == "https://api.example.com/list?category=all"
