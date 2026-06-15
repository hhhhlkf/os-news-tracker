# 个性化推荐、打分机制与新闻总结设计文档

**日期：** 2026-06-15
**状态：** 待用户评审
**项目：** `os-news-tracker` V2 功能升级

---

## 1. 背景与目标

在 V1 系统（新闻采集 + LLM 结构化入库 + 可检索 React 前端）的基础上，新增三个相互联动的子系统，将系统从「共享情报库」升级为「个性化情报助手」：

1. **用户系统** — 完整的注册/登录体系，支持多用户独立偏好
2. **个性化打分引擎** — 用户自定义评分准则，AI 对每条新闻按个人偏好评分（0-100），支持按需 LLM 精打
3. **新闻总结与趋势分析** — 按需触发（基于个人偏好）+ 定期自动生成全局 Digest，包含热点识别和新兴趋势分析

### 明确不做（保持聚焦）

- 邮件推送（V3）
- iWiki 联动（V3）
- 用户之间共享/协作偏好
- 复杂 RBAC（只有 `user` / `admin` 两级）
- 实时 WebSocket 推送

---

## 2. 总体架构

```
┌────────────────────────────────────────────────────────┐
│                     现有 V1 系统                        │
│  采集 → 规范化 → 去重 → LLM enrichment → items 入库    │
└────────────────┬───────────────────────────────────────┘
                 │
        ┌────────▼────────┐
        │  User 子系统     │  注册/登录，JWT，角色
        └────────┬────────┘
                 │
        ┌────────▼────────────────────┐
        │   个性化打分引擎             │
        │  ┌──────────────────────┐   │
        │  │  用户 Scoring Criteria │  │  JSONB 自定义准则列表
        │  └────────┬─────────────┘   │
        │  快速通道 │ 规则匹配算分      │  查询时实时，无 LLM
        │  精打通道 │ LLM ScoringAgent  │  后台批量，可选开启
        └────────┬─────────────────────┘
                 │
        ┌────────▼────────────────────┐
        │   Digest 子系统             │
        │  DigestAgent (多步 Chain)   │  聚合 → 主题识别 → 综合 → 格式化
        │  按需（个性化）+ 定期（全局）│
        └─────────────────────────────┘
```

---

## 3. 用户系统

### 3.1 认证机制

- **方案**：邮箱 + 密码，`bcrypt` 存密码哈希，**JWT（无状态 token）**
- **Token 有效期**：7 天，前端存 `localStorage`
- **角色**：`user`（默认）/ `admin`（可管理采集源、查看所有用户、配置定时 Digest）
- **注册方式**：开放自助注册，无审批流程

### 3.2 数据模型

```sql
users(
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email        VARCHAR UNIQUE NOT NULL,
  password_hash VARCHAR NOT NULL,
  display_name VARCHAR,
  role         VARCHAR DEFAULT 'user',    -- user | admin
  is_active    BOOLEAN DEFAULT TRUE,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  last_login_at TIMESTAMPTZ
)
```

### 3.3 用户 Profile 初始化

用户成功注册后，系统自动创建一条空的 `user_profiles` 行（`criteria=[]`，`enable_llm_scoring=false`，`min_score_threshold=25`）。用户首次访问 `/settings/profile` 时看到的是空的准则列表，引导其添加第一条准则或使用 AI 建议功能。

### 3.4 API 端点

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/auth/register` | 注册（邮箱+密码），同时创建空 user_profiles | 无 |
| POST | `/auth/login` | 登录，返回 JWT | 无 |
| GET | `/auth/me` | 当前用户信息 | 需登录 |
| PUT | `/auth/me` | 更新 display_name 等 | 需登录 |

### 3.5 鉴权中间件

- 所有 `/items`、`/facets`、`/digest`、`/users/me/*` 端点需要有效 JWT
- `Authorization: Bearer <token>` header
- 无效/过期 token → 401，前端重定向 `/login`
- 管理员专属路由 `/admin/*` → 403 for non-admin

### 3.6 前端新增

- `/login` — 登录页（邮箱/密码表单）
- `/register` — 注册页
- 全局 Header 显示 display_name + 退出按钮
- 未登录访问任意页面 → 跳转 `/login`

---

## 4. 个性化偏好 Profile

### 4.1 核心概念：Scoring Criteria

用户可自由定义任意数量的「评分准则」，每条准则描述「什么样的内容对我重要」。准则彼此独立、可单独开关，每条有自己的权重。

### 4.2 数据模型

```sql
user_profiles(
  user_id      UUID PRIMARY KEY REFERENCES users(id),
  criteria     JSONB NOT NULL DEFAULT '[]',     -- Criterion[] 见下
  free_text_description TEXT,                   -- 给 LLM 精打分用的自然语言描述
  enable_llm_scoring BOOLEAN DEFAULT FALSE,
  min_score_threshold INT DEFAULT 25,           -- 低于此分的 item 在个性化视图中隐藏
  updated_at   TIMESTAMPTZ DEFAULT NOW()
)
```

#### Criterion 结构（JSONB 数组元素）

```json
{
  "id":      "c-uuid-001",
  "label":   "内核性能关注",
  "enabled": true,
  "weight":  2.0,
  "match": {
    "keywords":      ["eBPF", "scheduler", "PREEMPT_RT"],
    "sub_tags":      ["kernel", "performance"],
    "main_category": ["OS性能发展"],
    "info_type":     [],
    "keywords_op":   "any"  -- any | all，默认 any
  }
}
```

- `weight` 范围：0.5 – 3.0（UI 以步进 0.5 的滑块呈现）
- `match` 各字段均可为空数组（即不限制该维度）
- `keywords_op`：`any`= 命中任一关键词即算，`all`= 全部命中才算

### 4.3 AI 辅助生成准则（ProfileAdvisor Agent）

触发时机：用户点击「AI 帮我生成偏好建议」。

```
输入：
  - 系统当前 top-50 技术热词（来自 sub_tags 聚合）
  - 用户的历史点击记录 user_item_interactions（如有）
  - 可选：用户输入的职能描述（如"OS 维护工程师，关注 RHEL 生态"）

输出（JSON）：
  - 建议的 Criterion[] 列表（含 label / match / weight）
  - 每条附一句理由说明

UI 交互：
  - 建议结果以卡片列表展示
  - 用户可逐条「接受」或「忽略」
  - 接受后追加到 criteria 列表，可继续编辑
```

### 4.4 用户行为记录

```sql
user_item_interactions(
  user_id     UUID REFERENCES users(id),
  item_id     INT REFERENCES items(id),
  action      VARCHAR,   -- view | bookmark
  interacted_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, item_id, action)
)
```

### 4.5 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/users/me/profile` | 获取当前用户 profile（含 criteria） |
| PUT | `/users/me/profile` | 全量更新 profile |
| POST | `/users/me/profile/suggest` | 触发 ProfileAdvisor Agent，返回建议准则列表 |
| POST | `/users/me/interactions` | 记录用户行为（view/bookmark） |

### 4.6 前端：偏好设置页面 `/settings/profile`

布局分三区块：

1. **我的评分准则**
   - 准则卡片列表：每条显示 label + 匹配条件摘要 + 权重滑块 + 开关 + 删除
   - 「添加准则」按钮 → 弹出表单（label + 各维度多选 + 权重）
   
2. **LLM 精打分**
   - 开关（默认关）
   - 开启后显示「我的关注描述」文本框（用于给 LLM 的 free_text_description）
   
3. **AI 建议区块**
   - 「AI 帮我生成偏好建议」按钮
   - 返回的建议准则以可折叠卡片列表展示
   - 每条有「接受」「忽略」操作

---

## 5. 个性化打分机制

### 5.1 快速通道（无 LLM，查询时实时计算）

对每条 item，遍历用户所有 `enabled=true` 的准则：

```python
def compute_fast_score(item: ItemSummary, criteria: list[Criterion]) -> int:
    raw = 0.0
    for c in criteria:
        if not c.enabled:
            continue
        strength = _match_strength(item, c.match, c.match.keywords_op)
        if strength > 0:
            raw += c.weight * strength

    # 系统重要度基础加分
    importance_bonus = {"高": 15, "中": 5, "低": 0}.get(item.importance, 0)
    score = min(100, int(raw * 30) + importance_bonus)
    return score

def _match_strength(item, match_rule, op) -> float:
    """0.0 – 1.0，多个 match 字段取平均命中率"""
    scores = []
    if match_rule.keywords:
        item_text = f"{item.title} {' '.join(item.sub_tags)}"
        hits = sum(1 for kw in match_rule.keywords if kw.lower() in item_text.lower())
        if op == "all" and hits < len(match_rule.keywords):
            return 0.0
        scores.append(hits / len(match_rule.keywords))
    if match_rule.sub_tags:
        overlap = len(set(match_rule.sub_tags) & set(item.sub_tags))
        scores.append(overlap / len(match_rule.sub_tags))
    if match_rule.main_category:
        scores.append(1.0 if item.main_category in match_rule.main_category else 0.0)
    if match_rule.info_type:
        scores.append(1.0 if item.info_type in match_rule.info_type else 0.0)
    return sum(scores) / len(scores) if scores else 0.0
```

> 快速通道分数在查询时计算，**不持久化存储**。

### 5.2 精打通道（LLM，后台批量，可选）

当 `enable_llm_scoring=true` 时，后台 `ScoringAgent` 定期运行（每次新 items 入库后触发）：

**输入（每条 item）：**
```
用户描述：{free_text_description}

文章信息：
标题：{title_tldr}（{title}）
摘要：{summary}
关键词：{sub_tags}
分类：{main_category} / {info_type}

请给出 0-100 的相关性分数，以及一句理由。
只输出 JSON：{"score": 85, "reason": "与你关注的 eBPF 调度优化高度相关"}
```

**存储：**
```sql
user_item_scores(
  user_id    UUID REFERENCES users(id),
  item_id    INT REFERENCES items(id),
  score      INT NOT NULL,          -- 0-100
  scoring_method VARCHAR,           -- fast | llm
  score_reason TEXT,                -- LLM 方法时有
  scored_at  TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, item_id)
)
```

查询时：有 LLM 分数则优先使用，否则用快速通道分数。LLM 分数 72 小时后对新 items 重新评估。

### 5.3 性能说明

**快速通道**：在查询时对返回结果集（通常 ≤200 条）做 Python 内存计算，无额外 SQL。对于小型内部工具（数千 items，数十用户）性能足够。若未来 items 规模超过 1 万条且需要全局分数排序，可考虑将快速通道分数也物化到 `user_item_scores`（按需升级，V2 不做）。

**LLM 精打通道**：批量处理，在 items 入库后由后台任务触发（不阻塞 API），用户打开页面时如果分数已就绪则直接展示，否则降级显示快速通道分数。

### 5.4 个性化 Feed

登录用户访问 `/items` 时：

- 默认排序：按个性化分数（高→低），score 附在每条 item 响应中
- 可切换回原有排序（`published_at` / `fetched_at`）
- 侧边栏新增「最低相关度」滑块（对应 `min_score_threshold`，拖动即时过滤）
- 每条 item 右上角显示分数徽章（80+= 绿、50-79 = 蓝、＜50 = 灰）

**API 变化（`GET /items`）：**
- 登录状态下 response 增加 `personalized_score: int | null`
- 新增 query param：`sort_by=relevance`（登录后可用）、`min_score=N`

---

## 6. 新闻总结与趋势分析（Digest）

### 6.1 两种模式

| 模式 | 触发方式 | 内容范围 | 存储 |
|------|---------|---------|------|
| 个性化按需总结 | 用户主动生成 | 按用户 scoring criteria 筛选后的 items（≥ min_score_threshold） | 存入 `digests`，该用户可查 |
| 全局定时总结 | 系统自动（每日/每周） | 全量 items | 存入 `digests`，所有用户可查 |

按需生成时提供「扩展到全部内容」开关，允许用户切换至全局视角。

### 6.2 DigestAgent 多步 Chain

```
Step 1: 数据聚合
  输入：时间范围 + 用户 criteria（按需模式）或全量（定时模式）
  操作：
    - 查询/过滤 items（个性化模式先按 fast score 过滤）
    - 统计 sub_tag / main_category 频次分布
    - 对比「上一个同等时长区间」的频次（识别上升/下降趋势）
    - 取 top-30 items（按 importance + score 排序）
  输出：频次分布 JSON + trending deltas + top-30 item 摘要列表

Step 2: 主题识别 Agent（LLM Call #1）
  输入：频次分布 + trending deltas
  Prompt：分析当前技术热点（出现次数最多的 3-5 个主题）+ 潜在新兴主题（频次增长最快的 2-3 个）
  输出结构化 JSON：hotspots[] + emerging_topics[]

Step 3: 内容综合 Agent（LLM Call #2）
  输入：top-30 item 摘要 + Step 2 主题识别结果
  Prompt：基于以上信息写一段综合总结，针对 OS maintainer，说明这段时间的整体技术动向
  输出：period_summary（300 字以内）

Step 4: 格式化与存储
  拼装最终 Digest JSON → 写入 digests 表（status: ready）
```

### 6.3 Digest 输出结构（存入数据库）

```json
{
  "period_summary": "本周共收录 127 条信息。内核调度与 eBPF 工具链持续活跃...",
  "hotspots": [
    {
      "topic": "Linux 6.12 内核发布",
      "item_count": 8,
      "reason": "sched_ext 合入主线，多家评测对比发布",
      "related_item_ids": [12, 45, 67]
    }
  ],
  "emerging_topics": [
    {
      "topic": "RISC-V 服务器生态",
      "trend": "rising",
      "reasoning": "本周提及 4 次，较上周增加 3 次，多家厂商发布适配公告"
    }
  ],
  "stats": {
    "total_items": 127,
    "by_category": {"OS性能发展": 45, "OS跟踪来源": 38},
    "top_tags": [["kernel", 23], ["RHEL", 18], ["eBPF", 12]]
  }
}
```

### 6.4 数据模型

```sql
digests(
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trigger_type     VARCHAR NOT NULL,   -- manual | scheduled
  created_by       UUID REFERENCES users(id),  -- NULL for system-generated
  time_range_start TIMESTAMPTZ NOT NULL,
  time_range_end   TIMESTAMPTZ NOT NULL,
  scope            VARCHAR DEFAULT 'personalized',  -- personalized | global
  period_summary   TEXT,
  hotspots         JSONB DEFAULT '[]',
  emerging_topics  JSONB DEFAULT '[]',
  stats            JSONB DEFAULT '{}',
  status           VARCHAR DEFAULT 'generating',  -- generating | ready | failed
  error_message    TEXT,
  created_at       TIMESTAMPTZ DEFAULT NOW()
)
```

### 6.5 异步生成流程

DigestAgent 涉及 2 次 LLM 调用（约 15-40 秒），采用与现有 manual news run 相同的**轮询模式**：

1. POST `/digest` → 立即返回 `{id, status: "generating"}`，后台启动 DigestAgent
2. 前端每 2 秒轮询 GET `/digest/{id}`，直到 `status == "ready"` 或 `"failed"`
3. 前端在 Digest 详情区展示「生成中…」进度提示（显示当前步骤：Step 1/2/3）

### 6.6 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/digest` | 触发生成（传 time_range_start, time_range_end, scope=personalized\|global）→ 立即返回 `{id, status}` |
| GET | `/digest` | 列出所有可查看的 Digest（含全局 + 本人创建） |
| GET | `/digest/{id}` | 获取 Digest 详情（含状态） |
| GET | `/admin/digest/schedule` | 查看/配置定时任务（admin only） |
| PUT | `/admin/digest/schedule` | 设置每日/每周定时 Digest（admin only） |

### 6.6 前端：Digest 页面 `/digest`

布局：
1. **顶部操作栏**
   - 时间范围选择器（预设：今天、本周、本月、自定义）
   - 「范围」切换：个性化 / 全部内容
   - 「生成总结」按钮（loading 状态显示进度步骤）

2. **历史 Digest 列表**（卡片形式）
   - 全局定时总结（打 `全局` 标签）
   - 我生成的个性化总结（打 `个性化` 标签）
   - 显示时间范围 + 条目数 + 创建时间 + 状态

3. **Digest 详情**（点击展开或跳转）
   - 综合总结文字段落
   - 热点卡片（含 item 数量，可点击跳转 items 列表）
   - 新兴趋势卡片（含趋势方向标签 ↑）
   - 统计数据（分类分布 + top tags 词频）

---

## 7. 数据模型变更汇总

### 新增表

```sql
-- 用户账户
users(id, email, password_hash, display_name, role, is_active, created_at, last_login_at)

-- 个性化偏好
user_profiles(user_id, criteria JSONB, free_text_description, enable_llm_scoring, min_score_threshold, updated_at)

-- LLM 精打分缓存
user_item_scores(user_id, item_id, score, scoring_method, score_reason, scored_at)

-- 用户行为记录
user_item_interactions(user_id, item_id, action, interacted_at)

-- Digest 记录
digests(id, trigger_type, created_by, time_range_start, time_range_end, scope, period_summary, hotspots, emerging_topics, stats, status, error_message, created_at)
```

### 现有表不变

现有的 `items`、`sources`、`tags`、`item_tags`、`item_sources` 等表无需修改。

---

## 8. LLM Agent 边界

| Agent | 模块路径 | 调用时机 | 输入 | 输出 |
|-------|---------|---------|------|------|
| Enricher（现有）| `app/processing/enricher.py` | 入库时，每条一次 | item 正文 | 结构化字段 |
| ScoringAgent（新）| `app/processing/scorer.py` | 后台批量，用户开启 LLM 打分时 | user profile + item 摘要 | 0-100 分 + 理由 |
| ProfileAdvisor（新）| `app/processing/profile_advisor.py` | 用户主动触发 | top-50 热词 + 行为历史 | 建议 Criterion[] |
| DigestAgent（新）| `app/processing/digest_agent.py` | 按需/定期 | 聚合数据 + top items | Digest JSON（两次 LLM Call） |

所有 Agent 均使用现有 `LlmClient`（OpenAI 兼容接口），设计上兼容未来替换为 LangChain/LangGraph。

---

## 9. 测试策略

| 类型 | 覆盖范围 |
|------|---------|
| 单元测试 | `compute_fast_score()` 纯函数（各种 criteria 组合），ScoringAgent mock LLM，ProfileAdvisor mock LLM，DigestAgent 各 Step 独立 mock |
| 集成测试 | 注册/登录 API，profile CRUD，`/items` 个性化排序，`/digest` 生成与查询 |
| 前端测试 | ProfileCriteria 组件渲染，打分徽章显示，Digest 状态切换 |

---

## 10. 迭代路线图更新

原 V1 → V2 路线调整：

| 阶段 | 内容 |
|------|------|
| V2（本文档）| 用户系统 + 个性化打分 + Digest 总结 |
| V3 | 邮件推送（订阅 digest 到邮箱） + iWiki 联动 |
| V4 | 采集源管理后台、抓取健康度监控 |
| V5 | 语义检索、关系图谱、协作偏好 |

---

## 11. 待确认项

- [ ] `display_name` 是否需要对其他用户可见（目前方案中 profile 是私有的）
- [ ] 定时 Digest 的默认频率是每日还是每周？（建议：每周，admin 可改）
- [ ] LLM 精打分的批次大小和调用限速（依赖内部 LLM 网关的实际限制）
- [ ] 注册是否需要邮箱格式验证（无需发验证邮件，仅格式校验）
