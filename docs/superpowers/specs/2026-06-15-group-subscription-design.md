# 群组订阅与分级权限设计文档

**日期：** 2026-06-15
**状态：** 待用户评审
**项目：** `os-news-tracker` V2 群组订阅层

> 本文档对 `2026-06-15-v2-complete-design.md` 中的用户/权限/来源模型进行扩展，引入三级角色 + 群组订阅体系，以共享爬取成本、支持团队定制化需求。
>
> **与 V2 complete design 冲突时，本文档优先。**

---

## 1. 背景与动机

V2 设计中，agent_crawl 来源是每个用户独立配置的，个性化需求导致每人各跑一次爬取，API 消耗随用户数线性增长。

实际场景：团队内有相似关注点的人可以共享一套爬取任务，在此基础上各自叠加个人过滤/打分偏好。这就需要一个**群组订阅层**：

- 爬取成本在群组层面共摊
- 团队可以统一限定内容范围（群级过滤）
- 个人仍然保留个性化打分和过滤权限

---

## 2. 三级角色模型

### 2.1 系统级角色（存于 `users.role`）

> 注意：V2 complete design 中的 `user` → 改名为 `subscriber`，`admin` → 改名为 `system_admin`。

| 角色 | 说明 |
|------|------|
| `subscriber` | 普通订阅者，可加入群组、设个人偏好 |
| `system_admin` | 系统管理员，拥有全部权限 |

### 2.2 群内角色（存于 `group_members.role`）

群内角色与系统角色**独立**，一个 subscriber 可以是群 A 的 group_admin，同时是群 B 的 member。

| 角色 | 说明 |
|------|------|
| `member` | 群成员，查看群内容、设个人偏好 |
| `group_admin` | 群管理员，管理群来源、成员、群级过滤 |

### 2.3 权限继承关系

```
system_admin
  ├─ 对所有群组拥有 group_admin 权限
  └─ 系统级独占权限：创建/删除群组、任命群管、管理全局来源池

group_admin（群内）
  ├─ 选配全局来源给本群
  ├─ 创建 agent_crawl 来源（归属本群）
  ├─ 邀请/移除群成员
  ├─ 设置群级过滤规则
  └─ 拥有 member 的全部权限

member（群内）
  ├─ 查看群内容（items 来自群订阅的来源）
  ├─ 查看群来源列表和群级过滤规则（只读）
  └─ 设置个人 Scoring Criteria、过滤条件、打分偏好
```

---

## 3. 数据模型

### 3.1 新增表

```sql
-- 群组
groups(
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        VARCHAR(200) UNIQUE NOT NULL,
  description TEXT,
  created_by  UUID REFERENCES users(id),   -- 创建者（sysadmin）
  is_active   BOOLEAN DEFAULT TRUE,
  created_at  TIMESTAMPTZ DEFAULT NOW()
)

-- 群成员
group_members(
  group_id  UUID REFERENCES groups(id) ON DELETE CASCADE,
  user_id   UUID REFERENCES users(id) ON DELETE CASCADE,
  role      VARCHAR(20) DEFAULT 'member',   -- member | group_admin
  joined_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (group_id, user_id)
)

-- 群的来源订阅（哪些 source 归属哪个群）
group_sources(
  group_id   UUID REFERENCES groups(id) ON DELETE CASCADE,
  source_id  INT REFERENCES sources(id) ON DELETE CASCADE,
  added_by   UUID REFERENCES users(id),
  added_at   TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (group_id, source_id)
)

-- 群级过滤规则
group_profiles(
  group_id        UUID PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
  filter_criteria JSONB NOT NULL DEFAULT '{}',
  updated_by      UUID REFERENCES users(id),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
)
```

### 3.2 `filter_criteria` 结构（JSONB）

```json
{
  "keyword_whitelist": ["kernel", "CVE", "RHEL"],
  "keyword_blacklist": ["招聘", "入门教程", "活动通知"],
  "category_whitelist": ["OS性能发展", "OS跟踪来源"],
  "min_importance": "低"
}
```

字段语义：
- `keyword_whitelist`：非空时，item 标题或摘要中必须命中至少一个关键词才保留（OR 逻辑）；空数组 = 不限
- `keyword_blacklist`：命中任一则排除；空数组 = 不排除
- `category_whitelist`：限定主分类（空 = 不限）
- `min_importance`：最低重要度门槛（`高` > `中` > `低`）

### 3.3 现有表变更（最小化）

```sql
-- 1. users.role 枚举值重命名（Alembic migration，数据迁移 + 列 CHECK 约束更新）
--    现有 DB 中：'user' 改为 'subscriber'，'admin' 改为 'system_admin'
--    迁移 SQL：
--      UPDATE users SET role = 'subscriber'    WHERE role = 'user';
--      UPDATE users SET role = 'system_admin'  WHERE role = 'admin';
--    代码层：app/enums.py 中 UserRole 枚举值同步更新，所有 role 比较改用枚举

-- 2. agent_source_configs 新增 group_id（agent_crawl 来源必须归属一个群组）
ALTER TABLE agent_source_configs
  ADD COLUMN group_id UUID REFERENCES groups(id) ON DELETE SET NULL;
```

---

## 4. 来源管理权限

### 4.1 全局来源池（RSS / page_monitor / search / api）

由 sysadmin 维护，存于 `sources` 表，无群归属。群管理员从全局池**选配**来源到本群：向 `group_sources` 写入一行，不创建新的 source 记录。

### 4.2 Agent_crawl 来源（智能爬取）

由 group_admin 创建，必须指定所属群组（`group_id`），自动写入 `group_sources`。一个 agent_crawl source 只属于一个群组。

sysadmin 可以为任意群创建 agent_crawl 来源。

### 4.3 来源归属总览

| Source 类型 | 创建者 | 是否全局 | 归属群 |
|-------------|-------|---------|-------|
| rss / page_monitor / search / api | sysadmin | 是，全局池 | 通过 group_sources 关联 |
| agent_crawl | group_admin / sysadmin | 否 | 创建时指定 group_id |

---

## 5. Feed 数据流

登录用户的内容 Feed 按三层叠加过滤：

```
Step 1: 确定用户可见的 source 集合
  SELECT DISTINCT gs.source_id
  FROM group_sources gs
  JOIN group_members gm ON gs.group_id = gm.group_id
  WHERE gm.user_id = ?

Step 2: 查询 items + 群级过滤
  - SQL：SELECT * FROM items WHERE source_id IN (Step 1 结果)
  - Python 内存过滤：
      for each item:
        找出该 item.source_id 对应的所有群组 (group_sources)
        与用户加入的群组取交集 → 得到「相关群集合」
        → 对相关群集合中的每个群，检查 item 是否通过该群的 filter_criteria
        → 若通过任一群的过滤，保留该 item（OR 逻辑，宽松处理）

  **说明**：当同一个来源被多个群订阅时，采用"宽松 OR"语义——
  用户只要通过其中任意一个群的过滤规则即可看到该 item。
  这意味着：群 A 过滤掉的内容，若群 B 允许且用户同时在群 B，依然可见。
  这是有意的设计——用户加入的群越多，能看到的内容越广。
  若将来需要更严格的策略（必须通过所有群的过滤），可改为 AND 逻辑。
  - 过滤规则评估（顺序，任一失败则 discard）：
      1. min_importance 门槛（高=3 中=2 低=1）
      2. category_whitelist（空=跳过）
      3. keyword_blacklist（命中则 discard）
      4. keyword_whitelist（非空时须命中至少一个，否则 discard）

Step 3: 个人打分
  compute_fast_score(item, user_profile.criteria) → personalized_score
  按 personalized_score 或 published_at 排序，返回前端
```

### 未加入任何群组的用户

未加入任何群组 → Feed 为空（返回空列表 + 提示语「你还没有加入任何群组，请联系管理员」）。

---

## 6. API 端点完整列表（新增 + 修改）

### 6.1 系统管理员专用

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/groups` | 创建群组（body: name, description, admin_user_id）|
| GET | `/admin/groups` | 列出所有群组 |
| GET | `/admin/groups/{id}` | 查看群组详情（含成员数、来源数）|
| PUT | `/admin/groups/{id}` | 更新群组信息（name, description, is_active）|
| DELETE | `/admin/groups/{id}` | 删除群组（级联删除 group_members, group_sources）|
| PUT | `/admin/groups/{id}/admins/{user_id}` | 设置用户为 group_admin |
| DELETE | `/admin/groups/{id}/admins/{user_id}` | 撤销 group_admin 角色（降为 member）|
| GET | `/admin/sources` | 查看全局来源池 |

### 6.2 群管理员操作（group_admin + sysadmin）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/groups/{id}/members` | 查看成员列表 |
| POST | `/groups/{id}/members` | 邀请用户加入（body: user_id）|
| DELETE | `/groups/{id}/members/{user_id}` | 移除成员 |
| GET | `/groups/{id}/sources` | 查看群来源列表 |
| GET | `/sources/global` | 查看可选配的全局来源（rss/page_monitor/search/api）|
| POST | `/groups/{id}/sources/{source_id}` | 将全局来源关联到本群 |
| DELETE | `/groups/{id}/sources/{source_id}` | 解除来源关联 |
| GET | `/groups/{id}/profile` | 查看群级过滤规则（成员也可查看）|
| PUT | `/groups/{id}/profile` | 更新群级过滤规则 |
| POST | `/sources/agent` | 创建 agent_crawl 来源（body 必须含 group_id）|

### 6.3 普通成员

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/groups/my` | 列出我加入的所有群组 |
| GET | `/groups/{id}` | 查看群组基本信息（仅成员可见）|
| GET | `/groups/{id}/sources` | 查看群来源列表（只读）|
| GET | `/groups/{id}/profile` | 查看群级过滤规则（只读）|

### 6.4 修改现有端点

| 端点 | 变化 |
|------|------|
| `GET /items` | 自动按 Step 1-3 过滤，未加群用户返回空 |
| `GET /facets` | 只统计用户群内容的分面 |
| `GET /digest` | 个性化 Digest 基于用户群内容（非全量）|
| `POST /sources/agent` | 新增必填字段 `group_id` |

---

## 7. 权限校验矩阵

| 操作 | subscriber | group_admin（本群）| system_admin |
|------|-----------|-------------------|-------------|
| 查看自己的 Feed | ✅ | ✅ | ✅ |
| 设置个人 Scoring Criteria | ✅ | ✅ | ✅ |
| 查看群来源列表 | ✅（仅本群）| ✅（本群）| ✅（所有群）|
| 查看群过滤规则 | ✅（只读）| ✅（可编辑）| ✅（可编辑）|
| 关联全局来源到群 | ❌ | ✅ | ✅ |
| 创建 agent_crawl 来源 | ❌ | ✅ | ✅ |
| 邀请/移除成员 | ❌ | ✅（本群）| ✅（所有群）|
| 创建/删除群组 | ❌ | ❌ | ✅ |
| 任命/撤销群管理员 | ❌ | ❌ | ✅ |
| 管理全局来源池 | ❌ | ❌ | ✅ |

---

## 8. 前端变化

### 新增页面/组件

| 路由 | 说明 | 可见角色 |
|------|------|---------|
| `/admin/groups` | 群组管理列表（创建/删除/查看成员）| sysadmin |
| `/admin/groups/{id}` | 群组详情（成员管理、来源管理、分配群管）| sysadmin |
| `/groups/my` | 我的群组列表 | 所有已登录 |
| `/groups/{id}` | 群组详情（来源列表 + 群过滤规则）| 群成员 |
| `/groups/{id}/sources` | 群来源管理（添加/删除 + 新建 agent 来源）| group_admin |
| `/groups/{id}/profile` | 群级过滤规则编辑 | group_admin |

### 现有页面调整

- **HomePage**：新增 Feed 来源标注（每条 item 显示来自哪个群组的哪个来源）；未加群时显示引导提示
- **Header Nav**：新增「我的群组」入口
- **AgentSourcesPage**：改为群维度，创建 agent 来源时需选择目标群组

---

## 9. 与 V2 Complete Design 的对照

| 功能 | V2 Original | 本文档变更 |
|------|-------------|-----------|
| 角色枚举 | `user / admin` | `subscriber / system_admin`（重命名） |
| 群内角色 | 无 | `member / group_admin`（新增）|
| Agent 来源归属 | 个人创建，个人使用 | 归属群组，群员共享 |
| Feed 内容范围 | 全量 items | 按用户群组的来源集合过滤 |
| 群级过滤 | 无 | group_profiles.filter_criteria（新增）|
| 个人打分/过滤 | 不变 | 在群级过滤之后叠加，不变 |
| Digest 范围 | 个性化=个人 criteria 过滤全量 | 个性化=个人 criteria 过滤群内容 |

---

## 10. 设计决策记录

以下已与用户确认，不再是待确认项：

| 问题 | 决策 |
|------|------|
| 加群流程 | 用户可查看公开群列表并提交申请，group_admin 审批通过后加入（双向：group_admin 也可主动邀请） |
| RSS source 是否可被多群订阅 | 允许，`group_sources` 无唯一约束限制 |
| 新用户是否自动加入默认群 | 否，新用户默认无群，Feed 为空，显示引导提示「联系管理员加入群组」 |
| Digest 定时生成范围 | 按群分别生成，每个群有独立的定时 Digest；全局 Digest（sysadmin 视角）作为可选功能 |

### 加群流程补充设计

因此需要新增「入群申请」相关数据结构：

```sql
group_join_requests(
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id    UUID REFERENCES groups(id) ON DELETE CASCADE,
  user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
  status      VARCHAR(20) DEFAULT 'pending',   -- pending | approved | rejected
  message     TEXT,                             -- 用户的申请理由（可选）
  reviewed_by UUID REFERENCES users(id),
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(group_id, user_id)    -- 同一用户对同一群只能有一条待处理申请
)
```

新增 API：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/groups` | 查看所有公开群列表（任意已登录用户）|
| POST | `/groups/{id}/join-requests` | 提交入群申请（body: message 可选）|
| GET | `/groups/{id}/join-requests` | 查看待审批申请列表（group_admin + sysadmin）|
| PUT | `/groups/{id}/join-requests/{request_id}` | 审批（body: status=approved\|rejected）（group_admin）|

**入群流程**：
```
用户申请 → pending 状态 → group_admin 审批
  → approved → 自动写入 group_members（role=member）
  → rejected → 申请记录保留，用户可重新申请（需等待冷却期或管理员删除旧申请）
```

### Digest 按群生成补充设计

每个群有独立的 Digest 配置，新增：

```sql
group_digest_schedule(
  group_id         UUID PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
  enabled          BOOLEAN DEFAULT FALSE,
  frequency        VARCHAR(20) DEFAULT 'weekly',   -- daily | weekly
  day_of_week      INT,                             -- 0-6，weekly 时有效（0=周一）
  hour_utc         INT DEFAULT 8,                   -- UTC 几点触发
  updated_by       UUID REFERENCES users(id),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
)
```

API：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/groups/{id}/digest/schedule` | 查看群 Digest 定时配置（成员可查）|
| PUT | `/groups/{id}/digest/schedule` | 更新配置（group_admin）|
| POST | `/groups/{id}/digest` | 手动触发群 Digest 生成（group_admin）|
| GET | `/groups/{id}/digests` | 查看本群的历史 Digest 列表 |
