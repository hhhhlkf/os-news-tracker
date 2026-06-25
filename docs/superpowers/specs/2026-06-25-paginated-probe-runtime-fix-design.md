# 分页式 Probe 运行时修复设计

**日期：** 2026-06-25
**状态：** 待用户评审
**项目：** `os-news-tracker` — 智能探测 + 一键 Agent 运行的分页链路修复

---

## 1. 背景与动机

针对 `https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10` 这类「带分页 query 参数的文章列表 API」，用户反馈：即便经过 2026-06-25 上一轮分页改造，一键 Agent 运行**仍然只爬第 1 页的 10 条**。

### 1.1 现状链路（两条独立逻辑）

**逻辑一：智能探测系统**（`backend/app/sources/api_discovery.py`）

```
Playwright 渲染页面
  → page.on("response") 捕获所有 JSON XHR/Fetch 响应
  → _find_list_arrays 递归找 list[dict] 数组
  → _evaluate_candidate 字段映射 + _score 评分
  → _build_probe 生成静态 probe 配置
  → _self_check_probe 自检（ApiAdapterFetcher 复跑，pagination 限制 1 页）
  → 返回 ApiDiscoveryResult
```

**逻辑二：一键 Agent 运行**（`backend/app/fetchers/agent_crawl.py`）

```
_build_plan
  ├─[有 cached probe]→ 直接复用，不重新探测
  └─[无 cached probe]→ _try_runtime_discovery
       → discover_api_source() 探测
       → 手拼 probe 写入 candidate source 的 api_config.probe（DB 缓存）
       → _build_plan_from_api → ApiAdapterFetcher.fetch
```

### 1.2 三个冲突点（上一轮已识别并修复了一半）

| # | 冲突点 | 上一轮是否修复 |
|---|--------|---------------|
| 1 | 探测快照把 `page=1&pageSize=10` 固化进 `probe.url`，运行侧 `ConfigurableApiProbeAdapter._fetch_json_list` 无翻页逻辑 | ✅ 修了 `_build_probe`（自动生成 `pagination`）+ `_fetch_json_list`（接入分页引擎） |
| 2 | `_score` 把 `blogByCategoryPage` 误判为分类元数据扣分 | ✅ 修了 `_score` |
| 3 | **运行时探测走的是 `_try_runtime_discovery`，它手拼 probe 时绕过了 `_build_probe`，且 `ApiDiscoveryResult` 不携带 `pagination`** | ❌ **未修，本轮核心** |

### 1.3 本轮发现的两个真实缺陷

**缺陷 A — `_try_runtime_discovery` 旁路了带分页的 `_build_probe`**

`agent_crawl.py:214-220` 在探测成功后**手动重新拼了一个 probe dict**：

```python
probe = {
    "mode": "json_list",
    "method": result.method,
    "url": result.api_url,        # 原始捕获 URL，仍带 page=1&pageSize=10
    "items_path": result.items_path or "",
    "fields": result.fields,
    # ❌ 没有 pagination 字段
}
```

它直接取 `ApiDiscoveryResult.api_url`（带分页参数的原始 URL），**完全不包含 `pagination`**。上一轮把 `pagination` 留在了 `_build_probe` 内部（仅供自检用），从未提升到 `ApiDiscoveryResult`，所以运行时探测命中的 probe 注定无分页配置，运行侧分页引擎不触发。

**缺陷 B — 旧的「无分页」probe 已缓存进 DB，复用逻辑不会重新探测**

`_build_plan`（`agent_crawl.py:134-177`）的复用顺序：先看 candidate source 的 `api_config.probe`，**只要有 probe 就直接复用、不重新探测**。OpenAnolis 源在上一轮之前已被探测过，DB 里缓存了一个不带 `pagination` 的旧 probe。即便本轮把代码修对，旧缓存仍在，运行时仍命中旧 probe、仍单页。这是「代码改对了但数据没更新」的缓存陷阱。

### 1.4 验证依据

- `_self_check_probe`（`api_discovery.py:535-537`）确实把 `pagination` 限制到 1 页做自检——说明自检阶段分页逻辑能跑、能产出 `pagination`，但**自检完就丢弃了**，没流到运行时。
- 上一轮 12 个单测全绿，是因为它们直接测 `_build_probe` 和 `ConfigurableApiProbeAdapter`，**没有覆盖 `_try_runtime_discovery → DB 缓存 → 复用` 这条端到端链路**——恰好是出问题的链路。

---

## 2. 修复目标

1. 让 `ApiDiscoveryResult` 携带 `pagination`，使运行时探测产出的 probe 自带分页配置。
2. 让 `_try_runtime_discovery` 把 `pagination` 写入缓存的 probe，不再手拼无分页 probe。
3. 让已缓存的旧「脏 probe」自动失效并触发重新探测，无需手动清 DB。
4. 补齐端到端链路测试，堵住上一轮盲区。

---

## 3. 整体设计

### 3.1 数据流（改后）

```
discover_api_source()
  → _build_probe（已能产出 pagination，上一轮已修）
  → _self_check_probe（pagination 限 1 页自检）
  → result.pagination = probe.get("pagination")   ★新增：回填
  → result.api_url = 剥离分页参数后的模板 URL     ★新增：干净 URL
       │
       ▼
_try_runtime_discovery()
  → probe = {..., "url": result.api_url, "pagination": result.pagination}  ★改：带 pagination
  → 写入 candidate source 的 api_config.probe（DB 缓存）
       │
       ▼
_build_plan（下次运行复用）
  ├─[cached probe 通过分页一致性校验]→ 直接复用
  └─[cached probe 是脏的：URL 有分页参数但无 pagination]→ 丢弃，触发重探  ★新增
       │
       ▼
_build_plan_from_api → ApiAdapterFetcher.fetch
  → _fetch_json_list_paginated（上一轮已修，逐页翻页）
```

### 3.2 改前 vs. 改后对比

| 层面 | 改前 | 改后 |
|------|------|------|
| `ApiDiscoveryResult` | 不含 `pagination` | 新增 `pagination: dict \| None` 字段，`to_dict()` 暴露 |
| `discover_api_source` 主流程 | `result.api_url = best.api_url`（原始 URL）；丢弃 `pagination` | `result.api_url` = 剥离分页参数后的 URL；`result.pagination = probe.get("pagination")` |
| `_try_runtime_discovery` | 手拼无 `pagination` 的 probe | 写入 `result.pagination`（若有） |
| `_build_plan` 复用 | 有 probe 即复用，不校验 | 复用前做分页一致性校验，脏 probe 触发重探覆盖 |
| 端到端测试 | 无 | 覆盖探测→缓存→复用全链路 |

---

## 4. 详细改动

### 4.1 `backend/app/sources/api_discovery.py`

**（a）`ApiDiscoveryResult` 增加 `pagination` 字段**

```python
@dataclass
class ApiDiscoveryResult:
    root_url: str
    success: bool = False
    api_url: str | None = None
    method: str = "GET"
    items_path: str | None = None
    fields: dict = field(default_factory=dict)
    pagination: dict | None = None          # ★新增
    name_suggestion: str = ""
    # ... 其余字段不变

    def to_dict(self) -> dict:
        return {
            # ... 其余键不变
            "pagination": self.pagination,   # ★新增
            # ...
        }
```

**（b）`discover_api_source` 主流程回填 `pagination` 并用干净 URL**

在「Success — use this candidate」分支（约 `api_discovery.py:622`）：

```python
result.success = True
result.api_url = _strip_pagination_params(best.api_url, probe)  # ★改：剥离分页参数
result.method = best.method
result.items_path = best.items_path or ""
result.fields = probe["fields"]
result.pagination = probe.get("pagination")                       # ★新增：回填
# ... 其余赋值不变
```

新增辅助函数 `_strip_pagination_params(api_url, probe)`：若 `probe` 含 `pagination`，用已有的 `_strip_query_param` 从 `api_url` 移除 `page_param` 和 `size_param`；否则原样返回。这样 `result.api_url` 存的是「去分页参数后的模板 URL」，下游 probe.url 自然干净，运行侧逐页注入 `page=N` 时不与残留参数冲突。

> **设计选择**：选「剥离后干净 URL」而非「原始 URL + 运行侧逐页覆写」。理由：干净 URL 让 probe 配置语义清晰（`url` = 模板，`pagination` = 翻页规则），日志/前端展示不会出现 `page=1` 残留误导；且 `_url_with_default_query` 的「仅当 key 不存在才注入」语义在干净 URL 下行为更可预测。

### 4.2 `backend/app/fetchers/agent_crawl.py`

**（a）`_try_runtime_discovery` 写入 `pagination`**

`agent_crawl.py:214-220` 改为：

```python
probe = {
    "mode": "json_list",
    "method": result.method,
    "url": result.api_url,          # 已是剥离分页参数后的干净 URL
    "items_path": result.items_path or "",
    "fields": result.fields,
}
if result.pagination:               # ★新增
    probe["pagination"] = result.pagination
```

**（b）`_build_plan` 增加分页一致性校验（旧 probe 自动失效重探）**

`_build_plan` 当前的复用判定（`agent_crawl.py:149-165`）是「只要能取到 probe（自己的或借 candidate 的）就直接 `_build_plan_from_api`」。需要在校验通过后才走这条路：取到 `probe` 后，若 `_is_stale_paginated_probe(probe)` 为真，则**不进入 `_build_plan_from_api`**，让流程继续向下落到 `_try_runtime_discovery`（`agent_crawl.py:172`）重探。

把 `agent_crawl.py:162-165` 改为：

```python
if not probe:
    probe = api_config.get("probe")
if isinstance(probe, dict) and _is_stale_paginated_probe(probe):
    append_run_log(
        "plan", "检测到分页式 probe 缺失 pagination 配置，触发重新探测",
        source=source.name, url=probe.get("url") or source.url, level="warning",
    )
    # 不进入 _build_plan_from_api，落到下文 _try_runtime_discovery 重探
    probe = None
elif isinstance(probe, dict) or (
    source.api_config
    and isinstance(source.api_config.get("probe"), dict)
    and not _is_stale_paginated_probe(source.api_config.get("probe"))
):
    return self._build_plan_from_api(source, config)
```

> **逻辑说明**：
> - 第一个 `if`：`probe` 是脏 probe（URL 有分页参数且无 `pagination`）时，打日志并把 `probe` 置 None，**不 return**，让流程继续向下落到 `_try_runtime_discovery`（`agent_crawl.py:172`）重探。
> - `elif`：仅当 `probe` 是干净 dict，或 `source.api_config` 里的 probe 是干净 dict 时，才走 `_build_plan_from_api`。对 `source.api_config` 里的 probe 同样做 `_is_stale_paginated_probe` 复查，防止「`probe` 变量被置 None 但 source 上还挂着脏 probe」导致误短路。
> - 重探成功后 `_try_runtime_discovery`（`agent_crawl.py:230`/`:242`）用带 `pagination` 的新 probe 覆盖旧缓存；重探失败则按现有逻辑继续回退 LLM PlanAgent（`agent_crawl.py:177`），不卡死。

新增辅助函数 `_is_stale_paginated_probe(probe) -> bool`：当 `probe` 的 `url` 的 query 串里含分页参数（`page`/`pageNo`/`pageNum`/`currentPage`/`current`/`p`/`pageIndex`/`pageSize`/`size`/`limit`/`per_page`/`perPage`/`count`/`rows` 之一）**且** `probe` 无 `pagination` 字段时返回 `True`。命中即判定为上一轮旧代码产出的脏 probe。

> **判定条件说明**：仅在「URL 有分页参数 且 无 pagination 配置」时触发重探。干净 probe（无分页参数，或带 pagination）不触发，避免每次运行都重探造成开销。重探一次后写入带 `pagination` 的新 probe，后续运行稳定复用。

### 4.3 不变项确认

- `_build_probe`（上一轮已修，自动生成 `pagination`）—— 不动。
- `_fetch_json_list_paginated`（上一轮已修，逐页翻页）—— 不动。
- `ConfigurableApiProbeAdapter` 分页引擎 —— 不动。
- `GenericJsonApiFetcher`（`generic_json_list` adapter 用的另一套，本身分页没问题）—— 不动。
- DB schema —— 不动（`api_config` 是 JSONB，probe 加 `pagination` 键无需迁移）。
- 前端 —— 不动。

---

## 5. 错误处理与边界

| 场景 | 行为 |
|------|------|
| `result.pagination` 为 None（非分页 API） | `_try_runtime_discovery` 不写 `pagination` 键，probe 行为同上一轮非分页场景，单请求 |
| 重探时 Playwright 不可用 / 探测失败 | 沿用现有回退：`_try_runtime_discovery` 返回 None → 落到 LLM PlanAgent（`agent_crawl.py:177`），不卡死 |
| 重探成功但新候选无分页参数 | 新 probe 无 `pagination`，`_is_stale_paginated_probe` 判定为 False（URL 无分页参数），稳定复用，不再重探 |
| 重探成功且新候选有分页参数 | 新 probe 带 `pagination`，`_is_stale_paginated_probe` 判定为 False，稳定复用 |
| `discover_api_source` 多候选，最佳候选无分页、次优有分页 | 沿用现有「按 score 取首个通过自检的候选」逻辑；分页信息随被选中的候选走，不额外干预候选选择 |
| POST body 里的分页参数 | 本轮不处理（`_infer_pagination` 仅识别 URL query 参数）。OpenAnolis 是 GET，POST 场景留作后续。`_is_stale_paginated_probe` 只看 URL query，不会误判 POST-body 分页 API 为脏 |

---

## 6. 测试计划

### 6.1 单元测试

**`backend/tests/unit/test_api_discovery.py`**

- `TestDiscoverResultPagination`：`ApiDiscoveryResult.to_dict()` 含 `pagination` 键。
- `discover_api_source` 成功路径（mock `_capture_and_render` + mock `_self_check_probe`）：返回的 `result.pagination` 非空且含正确的 `page_param`/`size_param`/`has_more_path`；`result.api_url` 已剥离分页参数（不含 `page=`/`pageSize=`）。
- `discover_api_source` 非分页 API 路径：`result.pagination` 为 None，`result.api_url` 原样保留。

**`backend/tests/unit/test_agent_crawl.py`**

- `_try_runtime_discovery` 写入 DB 的 probe **带 pagination**：mock `discover_api_source` 返回带 `pagination` 的 result，断言 candidate source 的 `api_config.probe["pagination"]` 与 result 一致。
- `_try_runtime_discovery` 非分页 result：DB 中 probe 不含 `pagination` 键。
- `_build_plan` 旧脏 probe 触发重探：预置一个 `url` 带 `page=1`、无 `pagination` 的 cached probe，mock `discover_api_source` 返回带 `pagination` 的新 result，断言运行后 DB 中 probe 被新值覆盖、`_build_plan_from_api` 用的是新 probe。
- `_build_plan` 干净 probe 不触发重探：预置一个带 `pagination` 的 cached probe，断言不调用 `discover_api_source`，直接复用。
- `_build_plan` 无分页参数的 probe 不触发重探：预置一个 `url` 无分页参数、无 `pagination` 的 cached probe，断言不重探、直接复用。

### 6.2 端到端覆盖（堵上一轮盲区）

新增一条端到端测试：mock `discover_api_source` + mock `ApiAdapterFetcher` 的多页响应，验证 `_try_runtime_discovery → 写 DB → _build_plan_from_api → 多页抓取` 全链路产出超过单页条数。

### 6.3 回归

- 上一轮的 12 个分页单测保持绿。
- 全量后端测试保持 349 passed（本轮新增测试除外）。

---

## 7. 明确不做

- 不改 POST body 分页参数推断（OpenAnolis 是 GET，POST 留后续）。
- 不改 `GenericJsonApiFetcher`（独立分页引擎，无此问题）。
- 不改前端、不改 DB schema。
- 不做「批量清理所有已缓存脏 probe」的运维脚本——靠运行时自动失效重探按需清理，避免一次性扫库的复杂度。
- 不引入 probe 版本号机制——`_is_stale_paginated_probe` 的结构性判定已足够识别本轮目标脏数据；版本号是更通用的方案但属过度设计（YAGNI）。

---

## 8. 涉及文件

| 文件 | 改动 |
|------|------|
| `backend/app/sources/api_discovery.py` | `ApiDiscoveryResult` 加 `pagination` 字段 + `to_dict`；`discover_api_source` 回填 `pagination`、`api_url` 用剥离后 URL；新增 `_strip_pagination_params` |
| `backend/app/fetchers/agent_crawl.py` | `_try_runtime_discovery` 写入 `pagination`；`_build_plan` 加分页一致性校验；新增 `_is_stale_paginated_probe` |
| `backend/tests/unit/test_api_discovery.py` | `TestDiscoverResultPagination` + `discover_api_source` 成功/非分页路径测试 |
| `backend/tests/unit/test_agent_crawl.py` | `_try_runtime_discovery` 写 pagination + 旧 probe 重探 + 干净 probe 不重探测试 |

---

## 9. 验证标准

- 针对带 `page=1&pageSize=10` 的分页式 API，一键运行后 `agent_crawl_runs.items_created` 可超过单页条数（取决于 `max_pages` 与 `has_more`/`total`）。
- DB 中该源的 `api_config.probe` 含 `pagination` 键，`probe.url` 不含 `page`/`pageSize` query 参数。
- 运行日志出现「检测到分页式 probe 缺失 pagination 配置，触发重新探测」仅一次（首次重探），后续运行不再出现。
- 全量后端测试通过。
