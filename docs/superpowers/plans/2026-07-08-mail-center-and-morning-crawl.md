# Mail Center And Morning Crawl Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在首页落地邮件任务中心与系统晨抓两大模块，完成从后端资源、调度、日志到前端交互窗口的整套可用功能。

**Architecture:** 方案以首页现有新闻筛选和 `discovery methods` 为核心，不引入新的筛选 DSL，也不复活旧 `/sources` 业务模型。邮件中心采用 `provider` 抽象加第一版 `SMTP provider`，系统晨抓围绕 `discovery methods` 做单例配置、运行记录和失败明细，并尽量复用现有共享日志能力。

**Tech Stack:** FastAPI, SQLAlchemy 2.0, APScheduler, React 19, TypeScript, TanStack Query, existing shared `run_logs`, discovery routes/service layer.

---

## File Structure

### Backend

- Create: `backend/app/api/mail_routes.py`
  - 邮件模板、立即发送、预定发送任务 API。
- Create: `backend/app/api/morning_crawl_routes.py`
  - 晨抓配置、今日状态、时间线、失败方式 API。
- Create: `backend/app/mail/provider.py`
  - `MailProvider` 抽象接口。
- Create: `backend/app/mail/smtp_provider.py`
  - 第一版 SMTP 发信实现。
- Create: `backend/app/mail/rendering.py`
  - 邮件 HTML 渲染与 preview context 组织。
- Create: `backend/app/mail/service.py`
  - 模板预览、立即发送、定时发送执行、日志写入。
- Create: `backend/app/morning_crawl/service.py`
  - 晨抓配置读取、执行 active discovery methods、打标、巡检补抓。
- Modify: `backend/app/models.py`
  - 新增邮件与晨抓相关表。
- Modify: `backend/app/schemas.py`
  - 新增邮件与晨抓请求/响应模型。
- Modify: `backend/app/scheduler.py`
  - 注册邮件定时发送任务、邮件巡检任务、晨抓主任务、晨抓巡检任务。
- Modify: `backend/app/entry.py`
  - 接入新 router 和调度初始化。
- Modify: `backend/app/api/main.py`
  - 注册新路由。
- Modify: `backend/app/api/discovery_routes.py`
  - 只在必要时抽出内部服务入口，避免晨抓通过 HTTP 反调 `/discovery/methods/{id}/fetch`。

### Frontend

- Create: `frontend/src/components/MailTaskCenter.tsx`
  - 邮件中心总弹窗，含左侧可折叠导航和三个子页状态切换。
- Create: `frontend/src/components/MailImmediateSendPanel.tsx`
  - 立即发送子页。
- Create: `frontend/src/components/MailTemplateListPanel.tsx`
  - 模板列表子页。
- Create: `frontend/src/components/MailScheduleListPanel.tsx`
  - 已预定发送子页。
- Create: `frontend/src/components/MorningCrawlModal.tsx`
  - 晨抓总弹窗，含左侧可折叠导航和两个子页。
- Create: `frontend/src/components/MorningCrawlStatusPanel.tsx`
  - 状态与配置子页。
- Create: `frontend/src/components/MorningCrawlTimelinePanel.tsx`
  - 时间线与失败方式子页。
- Modify: `frontend/src/pages/HomePage.tsx`
  - 接入两个新入口模块。
- Modify: `frontend/src/api/client.ts`
  - 增加邮件与晨抓 API client。
- Modify: `frontend/src/types.ts`
  - 增加模板、预定发送、晨抓配置、晨抓运行、日志类型。

### Verification

本仓库当前规则是不主动新增测试，因此此计划默认使用：

- 后端导入/路由级现有测试与 targeted smoke
- 前端 `npm run build`
- 本地手动联调

仅当实现过程中缺少最小保护且用户同意时，再补必要测试。

## Task 1: 邮件中心整体框架

**Files:**
- Create: `backend/app/api/mail_routes.py`
- Create: `backend/app/mail/provider.py`
- Create: `backend/app/mail/smtp_provider.py`
- Create: `backend/app/mail/service.py`
- Modify: `backend/app/models.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/main.py`
- Modify: `backend/app/entry.py`
- Create: `frontend/src/components/MailTaskCenter.tsx`
- Modify: `frontend/src/pages/HomePage.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Verify: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_entry_import.py -v`
- Verify: `cd frontend && npm run build`

- [ ] **Step 1: 扩展后端数据模型，加入邮件中心骨架表**

在 `backend/app/models.py` 中新增最小可用骨架：

```python
class MailTemplate(Base):
    __tablename__ = "mail_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(500))
    recipients_json: Mapped[list] = mapped_column(JSON, default=list)
    filter_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)
    last_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_send_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_send_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 2: 为 provider 抽象定义统一接口**

在 `backend/app/mail/provider.py` 中定义：

```python
class MailProvider(Protocol):
    def send(self, *, subject: str, html: str, recipients: list[str], from_email: str, from_name: str | None = None) -> None: ...
    def test_connection(self) -> None: ...
```

- [ ] **Step 3: 落第一版 SMTP provider**

在 `backend/app/mail/smtp_provider.py` 中实现一个通用 SMTP provider，配置字段固定为：

```python
SMTPConfig = TypedDict(
    "SMTPConfig",
    {
        "smtp_host": str,
        "smtp_port": int,
        "smtp_username": str,
        "smtp_password": str,
        "smtp_from_email": str,
        "smtp_from_name": str,
        "smtp_use_tls": bool,
        "smtp_use_ssl": bool,
    },
)
```

- [ ] **Step 4: 建立邮件服务总入口**

在 `backend/app/mail/service.py` 中先搭出服务骨架：

```python
class MailService:
    def __init__(self, db: Session, provider: MailProvider) -> None: ...
    def normalize_filter_snapshot(self, raw: dict) -> dict: ...
    def list_templates(self) -> list[MailTemplate]: ...
    def create_template(self, payload) -> MailTemplate: ...
```

- [ ] **Step 5: 增加基础 API**

在 `backend/app/api/mail_routes.py` 先只接入骨架路由：

```python
router = APIRouter(prefix="/mail", tags=["mail"])

@router.get("/templates")
def list_mail_templates(...): ...

@router.post("/templates", status_code=201)
def create_mail_template(...): ...
```

- [ ] **Step 6: 接入前端总弹窗和首页入口**

在 `frontend/src/components/MailTaskCenter.tsx` 中建立总弹窗状态：

```tsx
type MailTab = "immediate" | "templates" | "schedules";

export function MailTaskCenter(props: {
  open: boolean;
  onClose: () => void;
  homeFilters: ItemQueryParams;
}) { ... }
```

并在 `frontend/src/pages/HomePage.tsx` 中新增：

```tsx
const [mailOpen, setMailOpen] = useState(false);
...
<MailTaskCenter open={mailOpen} onClose={() => setMailOpen(false)} homeFilters={filtersAsQueryParams} />
```

- [ ] **Step 7: 跑后端导入与前端构建验证**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_entry_import.py -v`  
Expected: PASS，至少保证新 router / model / service 不破坏启动导入。

Run: `cd frontend && npm run build`  
Expected: PASS，首页和弹窗骨架类型通过。

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/mail_routes.py backend/app/mail/provider.py backend/app/mail/smtp_provider.py backend/app/mail/service.py backend/app/models.py backend/app/schemas.py backend/app/api/main.py backend/app/entry.py frontend/src/components/MailTaskCenter.tsx frontend/src/pages/HomePage.tsx frontend/src/api/client.ts frontend/src/types.ts
git commit -m "feat: add mail center framework"
```

## Task 2: 立即发送板块

**Files:**
- Create: `frontend/src/components/MailImmediateSendPanel.tsx`
- Create: `backend/app/mail/rendering.py`
- Modify: `backend/app/mail/service.py`
- Modify: `backend/app/api/mail_routes.py`
- Modify: `frontend/src/components/MailTaskCenter.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Verify: `cd frontend && npm run build`
- Verify: 手动联调 `立即发送`

- [ ] **Step 1: 在后端补 preview context 与 HTML 渲染器**

在 `backend/app/mail/rendering.py` 中建立：

```python
def build_mail_preview_context(*, filters: dict, items: list[Item], subject: str) -> dict: ...
def render_mail_html(context: dict) -> str: ...
```

context 中每条新闻至少输出：

```python
{
    "title": item.title,
    "reason": item.why_it_matters,
    "summary": item.summary,
    "key_points": item.key_points or [],
    "hotspots": [...],
    "source_url": item.url,
}
```

- [ ] **Step 2: 在服务层实现“基于当前筛选立即预览 / 立即发送”**

在 `backend/app/mail/service.py` 中增加：

```python
def preview_immediate_send(self, *, filter_snapshot: dict, subject: str, recipients: list[str]) -> dict: ...
def send_immediate(self, *, filter_snapshot: dict, subject: str, recipients: list[str]) -> dict: ...
```

这里必须先调用统一筛选快照解析，再复用 `/items` 等价查询逻辑取新闻。

- [ ] **Step 3: 暴露立即发送 API**

在 `backend/app/api/mail_routes.py` 中新增：

```python
@router.post("/immediate/preview")
def preview_mail_immediate(...): ...

@router.post("/immediate/send")
def send_mail_immediate(...): ...
```

- [ ] **Step 4: 在前端实现“当前筛选快照 + 发送设置 + HTML 预览”**

在 `frontend/src/components/MailImmediateSendPanel.tsx` 中接入：

```tsx
export function MailImmediateSendPanel(props: {
  homeFilters: ItemQueryParams;
  onSaveTemplateRequested: (draft: MailTemplateDraft) => void;
}) { ... }
```

需要呈现：

- 当前筛选快照
- 收件人输入
- 邮件标题输入
- 发送时间/频率字段只作为“保存为模板”的草稿来源
- HTML 预览

- [ ] **Step 5: 将立即发送页挂入总弹窗**

在 `frontend/src/components/MailTaskCenter.tsx` 中根据 `activeTab === "immediate"` 渲染：

```tsx
<MailImmediateSendPanel homeFilters={homeFilters} onSaveTemplateRequested={...} />
```

- [ ] **Step 6: 进行前端 build 与手动联调**

Run: `cd frontend && npm run build`  
Expected: PASS

Manual:
- 打开首页
- 进入邮件任务中心
- 检查当前筛选是否带入
- 点击预览
- 点击立即发送，看到成功/失败反馈

- [ ] **Step 7: Commit**

```bash
git add backend/app/mail/rendering.py backend/app/mail/service.py backend/app/api/mail_routes.py frontend/src/components/MailImmediateSendPanel.tsx frontend/src/components/MailTaskCenter.tsx frontend/src/api/client.ts frontend/src/types.ts
git commit -m "feat: add immediate mail send panel"
```

## Task 3: 模版列表板块

**Files:**
- Create: `frontend/src/components/MailTemplateListPanel.tsx`
- Modify: `backend/app/mail/service.py`
- Modify: `backend/app/api/mail_routes.py`
- Modify: `frontend/src/components/MailTaskCenter.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Verify: `cd frontend && npm run build`
- Verify: 手动联调模板列表

- [ ] **Step 1: 在服务层补齐模板 CRUD 与模板预览**

在 `backend/app/mail/service.py` 中增加：

```python
def get_template(self, template_id: int) -> MailTemplate: ...
def update_template(self, template_id: int, payload) -> MailTemplate: ...
def delete_template(self, template_id: int) -> None: ...
def preview_template(self, template_id: int) -> dict: ...
def send_template_once(self, template_id: int) -> dict: ...
```

- [ ] **Step 2: 补模板列表 API**

在 `backend/app/api/mail_routes.py` 中新增：

```python
@router.get("/templates/{template_id}")
def get_mail_template(...): ...

@router.put("/templates/{template_id}")
def update_mail_template(...): ...

@router.delete("/templates/{template_id}", status_code=204)
def delete_mail_template(...): ...

@router.post("/templates/{template_id}/preview")
def preview_mail_template(...): ...

@router.post("/templates/{template_id}/send")
def send_mail_template(...): ...
```

- [ ] **Step 3: 在前端实现模板列表左侧列表态**

在 `frontend/src/components/MailTemplateListPanel.tsx` 中实现：

```tsx
type TemplateListPanelProps = {
  collapsedNav: boolean;
  onCreateSchedule: (templateId: number) => void;
};
```

左侧显示：

- 模板名
- 过滤摘要
- 收件人数量
- 最近结果

- [ ] **Step 4: 实现右侧模板详情、模板动作、最近一次发送摘要**

同文件中右侧竖排模块包括：

- 已选模板详情
- 模板动作：立即发送 / 编辑模板 / 创建预定发送
- 最近一次发送摘要

- [ ] **Step 5: 将模板页挂入总弹窗**

在 `frontend/src/components/MailTaskCenter.tsx` 中根据 `activeTab === "templates"` 渲染：

```tsx
<MailTemplateListPanel collapsedNav={navCollapsed} onCreateSchedule={...} />
```

- [ ] **Step 6: 验证模板列表流程**

Manual:
- 从立即发送页保存模板
- 在模板列表页看见新模板
- 打开详情
- 编辑模板
- 删除模板
- 立即发送一次

- [ ] **Step 7: Commit**

```bash
git add backend/app/mail/service.py backend/app/api/mail_routes.py frontend/src/components/MailTemplateListPanel.tsx frontend/src/components/MailTaskCenter.tsx frontend/src/api/client.ts frontend/src/types.ts
git commit -m "feat: add mail template list panel"
```

## Task 4: 已预定发送板块

**Files:**
- Create: `frontend/src/components/MailScheduleListPanel.tsx`
- Modify: `backend/app/models.py`
- Modify: `backend/app/mail/service.py`
- Modify: `backend/app/api/mail_routes.py`
- Modify: `backend/app/scheduler.py`
- Modify: `frontend/src/components/MailTaskCenter.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Verify: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_entry_import.py -v`
- Verify: `cd frontend && npm run build`

- [ ] **Step 1: 在后端增加预定发送与执行记录模型字段**

确认 `backend/app/models.py` 已含：

```python
class MailSchedule(Base): ...
class MailDelivery(Base): ...
```

其中 `MailDelivery.trigger_type` 至少支持：

- `manual_send`
- `scheduled_send`
- `patrol_resend`

- [ ] **Step 2: 在服务层实现 schedule CRUD 与执行日志查询**

在 `backend/app/mail/service.py` 中增加：

```python
def list_schedules(self) -> list[MailSchedule]: ...
def create_schedule(self, payload) -> MailSchedule: ...
def update_schedule(self, schedule_id: int, payload) -> MailSchedule: ...
def pause_schedule(self, schedule_id: int) -> MailSchedule: ...
def resume_schedule(self, schedule_id: int) -> MailSchedule: ...
def send_schedule_now(self, schedule_id: int) -> dict: ...
def list_schedule_logs(self, schedule_id: int) -> list[dict]: ...
```

- [ ] **Step 3: 暴露预定发送 API**

在 `backend/app/api/mail_routes.py` 中新增：

```python
@router.get("/schedules")
def list_mail_schedules(...): ...

@router.post("/schedules", status_code=201)
def create_mail_schedule(...): ...

@router.get("/schedules/{schedule_id}")
def get_mail_schedule(...): ...

@router.put("/schedules/{schedule_id}")
def update_mail_schedule(...): ...

@router.post("/schedules/{schedule_id}/pause")
def pause_mail_schedule(...): ...

@router.post("/schedules/{schedule_id}/resume")
def resume_mail_schedule(...): ...

@router.post("/schedules/{schedule_id}/send-now")
def send_now_mail_schedule(...): ...

@router.get("/schedules/{schedule_id}/logs")
def list_mail_schedule_logs(...): ...
```

- [ ] **Step 4: 在调度器里注册邮件主任务与巡检任务**

在 `backend/app/scheduler.py` 中新增两个系统级任务入口：

```python
def run_mail_schedule_tick() -> None: ...
def run_mail_schedule_patrol() -> None: ...
```

不要为每条 schedule 单独注册 APScheduler job，而是统一巡检启用中的任务。

- [ ] **Step 5: 在前端实现已预定发送列表页**

在 `frontend/src/components/MailScheduleListPanel.tsx` 中实现：

- 左侧预定发送列表
- 右侧横排摘要条
- 任务详情
- 任务操作：暂停 / 立即补发 / 编辑时间与频率
- 最近执行记录日志 pane

- [ ] **Step 6: 将预定页挂入总弹窗并联通模板页“创建预定发送”**

在 `frontend/src/components/MailTaskCenter.tsx` 中：

- 允许模板页跳转到 schedules 页
- 把选中模板的默认值带进 schedule 创建表单

- [ ] **Step 7: 验证定时任务基本流程**

Manual:
- 从模板创建预定发送
- 在已预定发送页能看到任务
- 暂停与恢复生效
- 立即补发生效
- 执行记录日志刷新

- [ ] **Step 8: Commit**

```bash
git add backend/app/models.py backend/app/mail/service.py backend/app/api/mail_routes.py backend/app/scheduler.py frontend/src/components/MailScheduleListPanel.tsx frontend/src/components/MailTaskCenter.tsx frontend/src/api/client.ts frontend/src/types.ts
git commit -m "feat: add mail schedule panel"
```

## Task 5: 系统晨抓配置与执行窗口中的状态与配置

**Files:**
- Create: `backend/app/api/morning_crawl_routes.py`
- Create: `backend/app/morning_crawl/service.py`
- Create: `frontend/src/components/MorningCrawlModal.tsx`
- Create: `frontend/src/components/MorningCrawlStatusPanel.tsx`
- Modify: `backend/app/models.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/main.py`
- Modify: `backend/app/entry.py`
- Modify: `backend/app/scheduler.py`
- Modify: `frontend/src/pages/HomePage.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Verify: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_entry_import.py -v`
- Verify: `cd frontend && npm run build`

- [ ] **Step 1: 新增晨抓配置与运行记录模型**

在 `backend/app/models.py` 中新增：

```python
class MorningCrawlConfig(Base): ...
class MorningCrawlRun(Base): ...
class MorningCrawlRunMethod(Base): ...
```

`MorningCrawlRunMethod.method_id` 必须指向 `discovery methods` 体系，而不是旧 `sources`。

- [ ] **Step 2: 在服务层建立晨抓配置读取与今日状态聚合**

在 `backend/app/morning_crawl/service.py` 中增加：

```python
def get_or_create_config(db: Session) -> MorningCrawlConfig: ...
def get_morning_crawl_dashboard(db: Session) -> dict: ...
def update_morning_crawl_config(db: Session, payload) -> MorningCrawlConfig: ...
```

- [ ] **Step 3: 增加晨抓状态与配置 API**

在 `backend/app/api/morning_crawl_routes.py` 中新增：

```python
router = APIRouter(prefix="/system-morning-crawl", tags=["system-morning-crawl"])

@router.get("")
def get_system_morning_crawl(...): ...

@router.put("")
def update_system_morning_crawl(...): ...

@router.post("/run-now")
def run_system_morning_crawl_now(...): ...
```

- [ ] **Step 4: 在调度器里注册晨抓主任务和晨抓巡检任务**

在 `backend/app/scheduler.py` 中新增：

```python
def run_system_morning_crawl() -> None: ...
def patrol_system_morning_crawl() -> None: ...
```

主任务只查询 `status=active` 的 discovery methods 并逐条执行。

- [ ] **Step 5: 在前端实现晨抓总弹窗与“状态与配置”子页**

在 `frontend/src/components/MorningCrawlModal.tsx` 和 `MorningCrawlStatusPanel.tsx` 中实现：

- 左侧可折叠导航
- 顶部状态指标
- 晨抓配置表单
- 规则解释区
- `立即执行一次`

- [ ] **Step 6: 在首页接入晨抓入口卡片**

在 `frontend/src/pages/HomePage.tsx` 中新增：

```tsx
const [morningCrawlOpen, setMorningCrawlOpen] = useState(false);
...
<MorningCrawlModal open={morningCrawlOpen} onClose={() => setMorningCrawlOpen(false)} />
```

- [ ] **Step 7: 验证晨抓状态与配置链路**

Manual:
- 打开晨抓窗口
- 查看今日状态
- 修改晨抓时间/频率/巡检间隔
- 立即执行一次
- 页面状态刷新

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/morning_crawl_routes.py backend/app/morning_crawl/service.py backend/app/models.py backend/app/schemas.py backend/app/api/main.py backend/app/entry.py backend/app/scheduler.py frontend/src/components/MorningCrawlModal.tsx frontend/src/components/MorningCrawlStatusPanel.tsx frontend/src/pages/HomePage.tsx frontend/src/api/client.ts frontend/src/types.ts
git commit -m "feat: add morning crawl status and config panel"
```

## Task 6: 系统晨抓配置与执行窗口中的时间线与失败方式

**Files:**
- Create: `frontend/src/components/MorningCrawlTimelinePanel.tsx`
- Modify: `backend/app/morning_crawl/service.py`
- Modify: `backend/app/api/morning_crawl_routes.py`
- Modify: `backend/app/scheduler.py`
- Modify: `frontend/src/components/MorningCrawlModal.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Verify: `cd frontend && npm run build`
- Verify: 手动联调晨抓日志页

- [ ] **Step 1: 在晨抓服务层落运行明细与失败记录**

在 `backend/app/morning_crawl/service.py` 中增加：

```python
def execute_morning_crawl(db: Session, *, trigger_type: str) -> MorningCrawlRun: ...
def list_morning_crawl_logs(db: Session, *, run_id: int | None = None) -> list[dict]: ...
def list_morning_crawl_failures(db: Session, *, run_id: int | None = None) -> list[dict]: ...
```

执行时要：

- 逐个运行 active discovery methods
- 写 `MorningCrawlRunMethod`
- 失败不阻断整体 run
- 只有整次成功结束才写当天成功标记

- [ ] **Step 2: 暴露时间线与失败方式 API**

在 `backend/app/api/morning_crawl_routes.py` 中新增：

```python
@router.get("/logs")
def get_system_morning_crawl_logs(...): ...

@router.get("/failures")
def get_system_morning_crawl_failures(...): ...

@router.get("/runs")
def list_system_morning_crawl_runs(...): ...

@router.get("/runs/{run_id}")
def get_system_morning_crawl_run(...): ...
```

- [ ] **Step 3: 在前端实现日志流样式的时间线与失败方式页**

在 `frontend/src/components/MorningCrawlTimelinePanel.tsx` 中实现：

- 左侧可折叠导航下的第二页
- 左边 `今日时间线` log pane
- 右边 `失败方式` log pane
- 数据按 run_id 或今日默认 run 查询

- [ ] **Step 4: 把第二页挂入晨抓总弹窗**

在 `frontend/src/components/MorningCrawlModal.tsx` 中根据 `activeTab === "timeline"` 渲染：

```tsx
<MorningCrawlTimelinePanel collapsedNav={navCollapsed} />
```

- [ ] **Step 5: 验证时间线与失败方式链路**

Manual:
- 执行一次晨抓
- 进入第二页
- 查看今日时间线
- 查看失败方式
- 检查日志顺序和状态标签

- [ ] **Step 6: Commit**

```bash
git add backend/app/morning_crawl/service.py backend/app/api/morning_crawl_routes.py backend/app/scheduler.py frontend/src/components/MorningCrawlTimelinePanel.tsx frontend/src/components/MorningCrawlModal.tsx frontend/src/api/client.ts frontend/src/types.ts
git commit -m "feat: add morning crawl timeline and failures panel"
```

## Spec Coverage Check

- 邮件任务中心整体框架：Task 1
- 立即发送板块：Task 2
- 模板列表板块：Task 3
- 已预定发送板块：Task 4
- 系统晨抓状态与配置：Task 5
- 系统晨抓时间线与失败方式：Task 6
- 统一筛选快照复用 `/items` 语义：Task 1 / Task 2 / Task 3 / Task 4
- `SMTP provider` 第一版：Task 1
- 晨抓只使用 `discovery methods`：Task 5 / Task 6
- 共享日志复用：Task 4 / Task 6

## Self-Review Notes

- 本计划故意没有为每个 task 追加新测试文件任务，因为当前仓库规则是不主动新增测试；验证以现有导入检查、前端 build 和手动联调为主。
- 任务边界严格按用户要求拆成 6 个板块任务，每个 task 都覆盖前后端，而不是只拆某一层。
- 晨抓相关任务没有使用旧 `/sources` 作为业务模型，只允许复用已有调度与日志实现思路。
