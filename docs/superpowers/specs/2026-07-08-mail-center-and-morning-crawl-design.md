# 邮件任务中心与系统晨抓设计

日期：2026-07-08

## 1. 背景与目标

本设计为首页新增的两个模块提供后端设计：

1. `邮件任务中心`
2. `系统晨抓配置与执行窗口`

这两个模块已经完成前端交互和界面方向确认，接下来需要将其收敛为可实现的后端资源模型与接口设计。

本设计只定义：

- 后端资源边界
- 数据模型建议
- API 设计
- 调度与日志行为
- 与现有新闻流、筛选、discovery 方法库的衔接方式

本设计不包含：

- 具体实现代码
- 前端实现细节
- 邮件服务供应商选型
- 旧新闻源体系的兼容方案

## 2. 核心约束

### 2.1 筛选条件必须复用现有 `/items` 参数语义

邮件模板与预定发送任务，不引入新的筛选 DSL。

统一保存一份与现有 `GET /items` 同构的筛选快照，字段语义与现有主页筛选保持一致，包括但不限于：

- `q`
- `main_category`
- `info_type`
- `importance`
- `sub_tag`
- `sort_by`
- `sort_dir`
- `published_after`
- `published_before`

### 2.2 相对时间必须动态求值

如果前端当前筛选是“最近 24h / 最近 7d”这类相对时间，模板或预定发送保存时不能固化为某一天的绝对日期。

必须保存为“相对时间定义”，并在真正发送时按当前时间重新换算。

### 2.3 定时爬取只使用 discovery 的爬取方式库

本设计明确废弃旧 `sources` 体系在“定时爬取”中的业务地位。

系统晨抓只面向：

- `抓取模块 · 爬取方式库`
- 即 `discovery methods`

旧的新闻源、标准源、历史 `/sources` 调度逻辑，只能作为技术实现参考，不得继续作为晨抓的业务主模型。

### 2.4 日志优先复用现有共享日志体系

邮件任务中心和系统晨抓不应各自再发明一套独立日志基础设施。

推荐复用现有共享日志能力，并在其上补充：

- 任务级过滤
- 资源级过滤
- 前端局部查询能力

## 3. 总体设计

本次新增三类后端资源：

1. `MailTemplate`：邮件模板
2. `MailSchedule`：预定发送任务
3. `MorningCrawlConfig` + `MorningCrawlRun`：系统晨抓配置与运行记录

三者关系如下：

- `MailTemplate` 保存“筛选定义 + 收件人 + 邮件标题”
- `MailSchedule` 保存“定时发送实例”，可来源于某个模板，也可以独立创建
- `MorningCrawlConfig` 是系统级单例配置
- `MorningCrawlRun` 是晨抓执行记录，记录当天执行状态、统计与失败信息

## 4. 统一筛选快照模型

### 4.1 目标

为邮件模板和预定发送提供统一的筛选快照对象，直接复用主页新闻流的筛选能力。

### 4.2 建议结构

```json
{
  "q": "kernel",
  "main_category": "OS 性能发展",
  "info_type": null,
  "importance": null,
  "sub_tag": "scheduler",
  "sort_by": "published_at",
  "sort_dir": "desc",
  "published_after_mode": "relative",
  "published_after_value": "24h",
  "published_after": null,
  "published_before_mode": "none",
  "published_before_value": null,
  "published_before": null
}
```

### 4.3 时间字段规则

支持三种时间模式：

1. `none`
2. `absolute`
3. `relative`

建议解释如下：

- `absolute`：使用 `published_after` / `published_before`
- `relative`：使用 `published_after_value` / `published_before_value`
- `none`：不加对应边界

推荐前期仅支持：

- `24h`
- `7d`
- `30d`

如果未来需要，再扩展更多相对时间范围。

### 4.4 后端求值方式

真正执行发送时，后端先将 `filter_snapshot` 解析为对现有 `/items` 查询逻辑等价的一组过滤参数：

- 相对时间换算成当前时刻对应的绝对边界
- 复用现有 Item 查询逻辑
- 返回最终命中的新闻条目

## 5. 邮件任务中心设计

### 5.1 目标

邮件任务中心需要支撑三个前端子页：

1. `立即发送`
2. `模板列表`
3. `已预定发送`

### 5.2 邮件模板模型

建议新增 `mail_templates` 表，字段包括：

- `id`
- `name`
- `subject`
- `recipients_json`
- `filter_snapshot_json`
- `is_active`
- `last_send_at`
- `last_send_status`
- `last_send_count`
- `created_at`
- `updated_at`

说明：

- `recipients_json` 为邮箱列表
- `filter_snapshot_json` 保存统一筛选快照
- `last_send_*` 只保存最近一次发送摘要，用于模板列表页顶部展示

### 5.3 预定发送任务模型

建议新增 `mail_schedules` 表，字段包括：

- `id`
- `template_id`，可为空
- `name`
- `subject`
- `recipients_json`
- `filter_snapshot_json`
- `frequency`
- `send_time`
- `enabled`
- `last_sent_at`
- `last_result_status`
- `last_result_count`
- `last_sent_marker_date`
- `next_run_at`
- `patrol_status`
- `created_at`
- `updated_at`

说明：

- `template_id` 用于表示该定时任务来源于哪个模板
- 创建后允许脱离模板独立存在，不强制双向实时同步
- `last_sent_marker_date` 用于支持“今日是否已发送”判断

### 5.4 邮件执行记录模型

建议新增 `mail_deliveries` 表，字段包括：

- `id`
- `schedule_id`，可为空
- `template_id`，可为空
- `trigger_type`
- `status`
- `item_count`
- `subject`
- `recipients_json`
- `filter_snapshot_json`
- `started_at`
- `finished_at`
- `error_message`

说明：

- `trigger_type` 取值建议：
  - `manual_send`
  - `scheduled_send`
  - `patrol_resend`
- 该表用于支撑“最近执行记录”日志与审计

### 5.5 邮件预览能力

发送前端需要看到 HTML 邮件预览，但不要求后端直接返回完整 HTML 字符串作为唯一结构。

推荐拆成两层：

1. `preview_context`
2. `rendered_html`

其中：

- `preview_context` 用于前端结构化展示
- `rendered_html` 用于真实邮件发送与前端预览复用

单条新闻需要输出以下字段：

- 标题
- 推荐理由
- 摘要
- 技术要点
- 技术热点
- 来源链接

## 6. 系统晨抓设计

### 6.1 目标

系统晨抓用于每天自动运行 `discovery methods`，抓取最近时间窗口内的信息，并维护：

- 今日状态
- 今日时间线
- 失败方式日志
- 每日标记
- 巡检补抓

### 6.2 晨抓配置模型

建议新增 `morning_crawl_configs` 表。

该表逻辑上是系统级单例，字段包括：

- `id`
- `enabled`
- `run_time`
- `frequency`
- `patrol_interval_hours`
- `time_window_mode`
- `time_window_value`
- `updated_at`

推荐默认值：

- `enabled = true`
- `run_time = 06:00`
- `frequency = daily`
- `patrol_interval_hours = 12`
- `time_window_mode = relative`
- `time_window_value = 24h`

### 6.3 晨抓运行记录模型

建议新增 `morning_crawl_runs` 表，字段包括：

- `id`
- `run_date`
- `trigger_type`
- `status`
- `started_at`
- `finished_at`
- `method_count`
- `fetched_count`
- `stored_count`
- `marker_written`
- `error_summary`

说明：

- `run_date` 用于标识“今天是否已经执行并打标”
- `trigger_type` 取值建议：
  - `scheduled`
  - `manual`
  - `patrol_repair`

### 6.4 晨抓方法执行明细

建议新增 `morning_crawl_run_methods` 表，字段包括：

- `id`
- `run_id`
- `method_id`
- `method_name`
- `status`
- `fetched_count`
- `stored_count`
- `error_message`
- `started_at`
- `finished_at`

说明：

- 这张表专门支撑“失败方式”列表
- 必须基于 `discovery methods` 记录

### 6.5 晨抓对象来源

晨抓时后端取数来源明确为：

- `discovery methods`
- 条件：`status=active`

不读取旧 `/sources` 的活动源列表。

### 6.6 晨抓执行规则

执行规则固定为：

1. 到设定时间后，执行全部 active discovery methods
2. 抓取时间窗口按“执行当下时间”回推
3. 执行完成后写入当天标记
4. 每隔 `patrol_interval_hours` 巡检一次
5. 若发现当天未打标，则立刻补抓并补写标记

## 7. API 设计

## 7.1 邮件模板

- `GET /mail/templates`
  - 返回模板列表
- `POST /mail/templates`
  - 创建模板
- `GET /mail/templates/{template_id}`
  - 返回模板详情
- `PUT /mail/templates/{template_id}`
  - 更新模板
- `DELETE /mail/templates/{template_id}`
  - 删除模板
- `POST /mail/templates/{template_id}/preview`
  - 返回当前模板命中的新闻预览与 HTML 预览
- `POST /mail/templates/{template_id}/send`
  - 立即发送一次
- `POST /mail/templates/{template_id}/schedule`
  - 基于模板创建预定发送任务

## 7.2 预定发送任务

- `GET /mail/schedules`
  - 返回预定发送列表
- `POST /mail/schedules`
  - 创建预定发送任务
- `GET /mail/schedules/{schedule_id}`
  - 返回任务详情
- `PUT /mail/schedules/{schedule_id}`
  - 修改任务设置
- `DELETE /mail/schedules/{schedule_id}`
  - 删除任务
- `POST /mail/schedules/{schedule_id}/pause`
  - 暂停任务
- `POST /mail/schedules/{schedule_id}/resume`
  - 恢复任务
- `POST /mail/schedules/{schedule_id}/send-now`
  - 立即补发
- `GET /mail/schedules/{schedule_id}/logs`
  - 返回最近执行日志

## 7.3 系统晨抓

- `GET /system-morning-crawl`
  - 返回当前配置与今日状态
- `PUT /system-morning-crawl`
  - 更新配置
- `POST /system-morning-crawl/run-now`
  - 立即执行一次
- `GET /system-morning-crawl/logs`
  - 返回今日时间线日志
- `GET /system-morning-crawl/failures`
  - 返回失败方式日志
- `GET /system-morning-crawl/runs`
  - 返回最近若干次运行记录
- `GET /system-morning-crawl/runs/{run_id}`
  - 返回某次运行的详情与方法级明细

## 8. 调度设计

### 8.1 邮件发送调度

建议在现有调度器内增加两类定时任务：

1. 预定发送任务调度
2. 预定发送巡检任务

推荐方式：

- 固定间隔巡检启用中的 `mail_schedules`
- 判断“当前是否到发送时间”
- 判断“今日是否已有发送标记”
- 若未发送，则执行并打标

这样比为每个任务动态注册大量 APScheduler job 更稳，更容易管理。

### 8.2 系统晨抓调度

系统晨抓是单例，不需要每个 discovery method 都注册单独调度项。

推荐只注册两个系统级任务：

1. 晨抓主任务
2. 晨抓巡检任务

晨抓主任务运行时，再去查询当前 active 的 discovery methods 并逐条执行。

### 8.3 执行串行策略

晨抓执行建议默认串行或小并发。

原因：

- `discovery methods` 的 fetch 可能较重
- 微信 / Playwright / 外部网页目标差异较大
- 串行更容易保证日志顺序与停止控制

前期建议默认串行，后续若性能不足，再引入受控并发。

## 9. 日志设计

### 9.1 复用共享日志

邮件发送与晨抓执行的底层日志，优先写入现有共享日志体系。

建议新增 `source` 或 `scope` 区分：

- `mail_template`
- `mail_schedule`
- `morning_crawl`

### 9.2 邮件任务日志

邮件日志需要能支撑前端：

- 最近执行记录
- 立即发送后的结果展示
- 预定发送的补发/失败记录

推荐按一次发送行为生成一组连续日志。

### 9.3 晨抓日志

晨抓日志需要分成两种视图：

1. 今日时间线
2. 失败方式

建议：

- 时间线基于 `morning_crawl_runs`
- 失败方式基于 `morning_crawl_run_methods`

前端仍可以最终以 log pane 样式渲染。

## 10. 与现有系统的衔接

### 10.1 与新闻流的衔接

邮件模板和发送任务，不需要再建新的新闻查询路径。

执行时直接调用与现有 `/items` 等价的查询逻辑，只是由后端内部完成参数求值和结果组织。

### 10.2 与 discovery 的衔接

系统晨抓只依赖：

- `discovery methods`
- `POST /discovery/methods/{id}/fetch` 对应的执行能力

推荐抽出内部服务层，而不是在调度器里直接反调 HTTP。

即：

- UI 仍走 HTTP 路由
- 调度与系统任务走内部 service

### 10.3 与旧 sources 的关系

旧 `/sources` 体系不再作为晨抓业务主模型。

设计文档、代码实现、前端文案都应避免再次把晨抓解释为“遍历旧 sources”。

## 11. 失败处理

### 11.1 邮件发送失败

发送失败时需要：

- 落库一条 `mail_deliveries`
- 写入失败日志
- 在模板/任务摘要中更新 `last_send_status`

不要求单个收件人级别的复杂回执系统，前期只关注整次发送成功或失败。

### 11.2 晨抓失败

如果某个 discovery method 失败：

- 不影响整次晨抓 run 继续处理其他方法
- 必须写入 `morning_crawl_run_methods`
- 必须进入“失败方式”视图

如果整次晨抓中断：

- 不写“今日成功标记”
- 留待巡检任务补抓

## 12. 实现优先级建议

推荐实施顺序：

1. 邮件模板资源层
2. 预定发送资源层
3. 邮件发送执行与预览
4. 系统晨抓配置与运行记录
5. 系统晨抓调度与巡检
6. 日志视图细化

原因：

- 邮件模板与预定发送是全新业务资源，需要先把模型定稳
- 晨抓虽然业务重要，但执行能力可复用现有 discovery fetch 逻辑

## 13. 非目标

本轮明确不做：

- 多用户权限隔离
- 每个收件人的个性化模板
- 邮件主题 A/B 测试
- 富文本编辑器
- 旧 `/sources` 与晨抓的双轨兼容
- 通用任务编排平台

## 14. 需要在实现前再次确认的点

虽然整体方向已确认，但实现前仍需在计划阶段明确：

1. 邮件发送底层使用哪种 SMTP / API provider
2. `MailSchedule` 是否允许完全脱离模板独立创建
3. 晨抓执行时是否需要“停止当前运行”能力
4. 晨抓日志是否要和 discovery 前端日志完全共用同一查询接口，还是仅共用底层存储

当前推荐默认答案：

1. 先抽象邮件发送 provider，具体实现后补
2. 允许独立创建，但前端主路径以“从模板创建”为主
3. 需要，但放入后续实现任务
4. 共用底层日志存储，但暴露独立业务接口
