# Discovery OpenHands 单 Agent Loop 与本地插件重构计划

## 1. 目标与范围

重构 OS News Tracker 最核心的智能探查能力：Agent 不再受固定抓取 Tool 和 DSL 表达能力限制，而是在受控沙箱中自主探查网站、编写 Python 采集器、执行验证并修复，最终沉淀为可审核、可版本化、可正式运行的本地插件制品。

本轮范围：

- **普通网站**：每个网站生成一个独立采集器制品。
- **微信公众号**：使用一个共享的无登录搜狗采集器，不为每个公众号复制 `crawler.py`。
- **内部论坛**：仅保留现有接口和分流位置，不参与本轮实现。
- **`agent_crawl`**：已经废弃，不修改、不复用，也不作为本方案参考架构。

正式抓取继续复用现有新闻处理链路：

```text
插件统一 JSON
→ CrawlOutputIngester
→ normalize
→ relevance-filter
→ dedup
→ enrich
→ store
```

正式**插件执行阶段**不调用 Agent、RAG 或 LLM；插件结果进入现有 Pipeline 后，Enricher 是否调用 LLM 保持当前行为不变。

## 2. 核心架构决策

采用项目内 **WebsiteLoopEngine 宿主控制层 + OpenHands Software Agent SDK 1.43.1**，不采用 Agent Graph、Graph Engineering、LangGraph 状态图或多 Agent handoff。OpenHands 替换普通网站旧的固定动作 Explore 和 JSON 源码 Build 内核，但不接管产品状态机、执行或验收。

```text
主程序 OS News Tracker
├── WebsiteLoopEngine（阶段、预算、状态、RAG、调度与裁决）
├── OpenHands Agent Runtime（Python 3.12，Explore + Build + Repair）
├── Sandbox Runtime（Docker + gVisor/runsc）
├── Connector Registry（插件版本与 Manifest）
├── Crawl Runtime（固定镜像中的通用 Runner）
└── 现有审核、抓取、Ingester 与新闻 Pipeline
```

Loop Engine 位于主程序中。RAG 检索、状态持久化、取消、检查点和审核编排都在主程序完成。OpenHands 的 `Conversation`、Terminal、FileEditor、Browser 以及 LLM tool loop 位于一次保留的 gVisor 容器内，直接编辑 `/workspace/crawler.py` 与 `/workspace/manifest-draft.json`；Explore、首次 Build 和后续 Repair 复用同一会话。禁止使用 OpenHands `DockerWorkspace`，也禁止 Agent 自行创建普通 Docker 容器。

Agent stdio 程序、可信 relay/proxy 程序与共享 egress policy 模块必须在构建时 COPY 进入不可变 OpenHands Runtime 镜像，安全关键字节由镜像 digest 覆盖。Python 3.12 依赖使用独立 fully-resolved `--require-hashes` lock，镜像同时保存 lock hash 与最终 Python 安装清单。运行时只创建并挂载每次运行的 allowlist 配置卷与短期 secret 卷，禁止由 backend 动态注入 Python 执行源码。

主程序使用独立且不可变的 `openhands-agent-runtime:1.43.1`（Python 3.12，`openhands-sdk==openhands-tools==openhands-agent-server==1.43.1`）。后端继续使用 Python 3.11+，正式 connector Execute 继续使用独立 `crawler-runtime`。两个 Runtime 不混装依赖。

OpenHands SDK/Chromium 会话使用独立的 `discovery_agent_sandbox_memory`（默认 `1g`）作为 Agent gVisor 容器上限；正式 connector Execute 继续使用 `discovery_sandbox_memory`（默认 `512m`），两者不得因 Agent 峰值需求而合并放宽。可信 relay/proxy 仍使用宿主固定的较小基础设施上限。Agent 协议 stdout 意外 EOF 时，宿主从 Docker process/inspect 生成有界且脱敏的 `exit_code`、`oom_killed` 证据，不接受 Agent 自报退出原因。

真实 LLM provider key 不进入 gVisor。每次运行由宿主生成短期随机 relay 凭证和非秘密 relay session identity；受信 egress-proxy 内的 LLM relay 才读取 provider key，并且只连接配置的精确 scheme/hostname/port 和固定 `/v1/chat/completions` 路径，使用显式不跟随 redirect 的传输，provider 3xx 一律 fail closed，绝不向其他 URL 重发 Authorization。relay 限制并发、请求总数、请求体和响应体，并从 provider response usage 记录可信累计 token；宿主按 relay sequence 增量读取，exact 通过 `(run_id, trusted provenance, relay_session_id, sequence)` 数据库唯一键原子写入 `LlmUsageEvent` 并同步增加 run usage，冲突即已确认且不会重复增加 UsageScope；同 run 的新 Agent session 使用新 identity，不与旧 sequence 冲突。unknown 也以同一唯一键写入零 token 的可信 durable audit marker 后才确认，不作估算。外部事务的提交/回滚始终由调用者所有，usage scope 每次从加锁的 run authoritative total 同步。每条成功、取消或失败终态路径都先 CAS 取得带 expiry 的 durable host claim 并持续心跳续租，再依次停止 Agent 和 relay、inspect 确认二者静止且不会产生新 usage（此时保留 relay 容器日志），提交全部 usage 与 `usage_drained` 阶段，删除资源并提交 `resources_removed`，最后才 Package 或写终态 run；watchdog/recovery 仅在 claim 过期后接管。acquire 只认空 owner 或数据库 wall clock 已过期的 lease，并生成不可复用随机 owner；renew 只认同 owner、未过期且 unfinished 的 run。PostgreSQL 使用 transaction-volatile `clock_timestamp()`，SQLite 检查使用兼容 wall-clock 表达式；lease 写入和过期比较始终来自同一数据库时钟。claim owner 同时作为 fencing token：heartbeat 失败会设置跨线程 lost 状态且终态线程等待 heartbeat 完整退出；Package 在 staging、方法写入、制品发布和终态 UPDATE 前检查，持锁时按数据库实时 wall clock 再续租，发布后再用 owner+未过期 lease 限定最终 UPDATE，旧 owner 不能 completed 或覆盖新 owner，迟到 renew 也不能在 terminal clear 后写回 claim。Package 补偿默认未确认：包括 staging 未返回句柄、committed probe、Session 创建/关闭或任一清理异常都保持 durable `finalization_pending`；只有已发布目录、staging 和 packaging DB row 的清理事务完整 commit 后才释放原 owner并安全写 failed，恢复器拒绝为补偿未确认的运行提前终态。失败、取消和 cleanup-degraded 的唯一终态事件、failed/cancelled run 与 claim 清理在同一事务提交。终态 checkpoint 暂时失败时持久化 `finalization_pending` 并由 worker/watchdog/startup recovery 幂等重试；Package pending 另存经宿主验证的相对制品路径、Manifest identity/checksum/signature/runtime、确定性评价与封装参数，checkpoint 损坏时只有重新验证这些字段和不可变 trial 后才能直接进入 Package，绝不退回 Explore。终态之后不再产生 usage side effect。checkpoint/final usage 只投影已确认的精确计量。流式末帧没有 usage 时只审计 unknown，不估算 token，也不接受 Agent 自报 usage。

OpenHands 会话清理是可重试状态机：先停止或隔离 Agent，再停止或隔离 relay，但保留 relay 容器及其日志；宿主随后通过 inspect 确认二者均已停止或不存在，从而证明不会再产生新的 usage。只有达到这一静止点，宿主才读取完整 trusted relay logs，并按 sequence 原子、幂等持久化所有 exact/unknown usage；`docker logs` 非零、超时或 OSError、exact usage 数据库提交失败、unknown 审计失败都会保持未确认。获得 `fully_drained_and_acked=true` 后才能删除 Agent/relay 容器及日志，并继续独立清理网络、配置卷和 secret 卷。这个“先静止、后 drain、最后删除”的顺序为 usage 日志建立封闭快照，消除了 drain 期间仍有 in-flight provider response 追加 usage、从而造成终态漏计的竞态。只有 inspect、全量 ack 和其余资源清理都成功后，才标记 closed、移除 active 记录并恰好释放一次容量 lease。任何 kill/rm/inspect/日志读取/本地进程清理不确定性都保留 session 引用、active run 和容量。主 Loop 连续重试仍无法确认时，把不含 secret/relay token 的 job、relay session identity、agent/proxy container、network、volume 和已确认 usage sequence 同时写入 checkpoint 与 run trace fallback，持久化脱敏 `openhands_cleanup_pending`/`openhands_cleanup_degraded` 事件并保持 run 非终态。受监管后台 worker 与启动/定时 recovery watchdog 都能接管；重启恢复同样先停止 Agent、再停止 relay，inspect 确认静止并保留 relay 容器日志，随后幂等提交剩余可信 usage，最后删除资源、刷新 checkpoint 并以 cleanup-degraded failed 收敛。checkpoint 缺失/损坏时使用 run trace fallback。未确认停止期间禁止写 completed、cancelled、普通 failed 或进入 Package；线程启动失败由当前宿主同步接管或后续 watchdog 恢复。

初始网络 allowlist 只包含用户入口 URL 的精确 hostname，不默认信任 `www`/非 `www` 别名。OpenHands Browser 的成功观察通过绑定当前 turn request ID 的逐事件协议，只提出结构化 URL/href/link、Markdown link target 与导航/redirect hostname 候选；失败或拒绝 observation 不提出候选。候选 stdout 不是授权事实：镜像内固定的可信 proxy policy 子命令必须独立对当前已允许 source URL 做公共地址固定解析与有界 GET，确认候选确实出现在 HTTP `Location`、HTML `href/src/action` 或结构化 JSON URL 字段后才原子热更新 allowlist。Agent 请求一个被拒绝的 hostname 本身永远不是扩权证据；无法独立复查的动态 JS network 候选在本阶段保守拒绝。

自动 Repair 的初始多域 allowlist 只能来自已审核 artifact manifest；checkpoint Resume 只能从 checksum/manifest 均重新验证通过的 trial artifact 恢复。普通手工 Discovery 无论 Agent 草稿或历史事件包含什么域，都只从精确 entry hostname 启动。

这不是简单 ReAct：循环边界、阶段、状态、轮次、时间预算、工具权限、确定性验收、检查点和发布门槛全部由程序控制，模型只负责需要判断与编写代码的部分。

## 3. Agent 阶段与职责

整个流程只有一个 Agent，各阶段由 Loop Engine 驱动，不拆成多个 Agent：

| 阶段 | 责任主体 | 职责 |
|---|---|---|
| Context | Loop Engine + Agent | 识别网站/微信类型，读取目标、约束和 RAG 经验 |
| Explore | OpenHands Agent | 在保留的 gVisor workspace 中使用 Terminal、FileEditor、Browser 探查列表、分页、详情页、字段与时间格式 |
| Build | OpenHands Agent | 直接生成或修改 `/workspace/crawler.py`、`manifest-draft.json` |
| Execute | Sandbox Runtime | 在 gVisor 中真实执行插件，不接受 Agent 自报的执行结果 |
| Evaluate | 确定性校验器 | 校验 JSON 契约、数量、字段、正文、去重、URL 可访问性和分页 |
| Repair | OpenHands Agent | 在同一会话中接收宿主真实 Execute/Evaluate 反馈并修改 workspace 文件 |
| Package | Loop Engine | 固化制品版本、校验和、依赖和审计证据，生成待审核方式 |
| Publish | 现有审核流程 | 人工批准后启用，未批准制品不得参加正式抓取 |

硬限制：

- 单次 Discovery 共享执行预算最多 30 分钟。
- Repair 次数由宿主做有界控制，模型 finish 只表示提交候选，绝不表示验收成功。
- 沙箱容量排队时间不计入 30 分钟，从获得容量后开始/恢复计时。
- OpenHands 保留会话全局最多占用 `total_capacity - 1` 个槽位；总容量小于 2 时启动 fail closed，确保宿主 Execute 始终保留一个非 Agent 槽位，正式/手动任务优先级不变。
- 超限后保留最近检查点和诊断，不继续无限尝试。

## 4. RAG：仅用于探查经验复用

RAG 只服务 Discovery，不进入正式抓取。

允许进入经验库的内容：

- 已审核通过的插件、网站技术特征和适用模式。
- 已确认的失败原因、修复办法和适用条件。
- 分页、时间解析、详情补抓、API/HTML/嵌入 JSON 等可复用模式。

禁止进入经验库的内容：

- 未验证的插件草稿和模型猜测。
- 未确认的失败结论。
- 网页新闻正文。
- Cookie、Token、Authorization、请求头等敏感数据。

首版采用轻量混合检索：

```text
域名与技术特征过滤
→ 关键词检索
→ Embedding 相似度
→ Top-K 经验交给 Agent
```

复用现有独立 Embedding Worker 和统一 Provider 能力，新增 Discovery 自己的经验表。首版数据规模较小，向量以 JSON 保存并在应用内计算相似度，不新增 pgvector；达到数万条经验后再评估迁移。

## 5. 插件制品

### 5.1 普通网站

每个网站一个版本化制品：

```text
backend/connectors/sites/<connector_key>/v<version>/
├── manifest.json
└── crawler.py
```

示例 Manifest：

```json
{
  "recipe_type": "python_plugin",
  "connector_key": "kernel_org",
  "version": 1,
  "entry": "https://www.kernel.org/",
  "entrypoint": "crawler:crawl",
  "runtime_version": "crawler-runtime:1",
  "checksum": "...",
  "allowed_domains": ["kernel.org", "www.kernel.org"]
}
```

### 5.2 微信公众号

微信公众号采用一个共享制品：

```text
backend/connectors/shared/wechat_sogou/v<version>/
├── manifest.json
└── crawler.py
```

每个公众号的 `CrawlMethod` 只保存公众号名称、关键词、分页等配置，统一调用共享 `crawler.py`：

```text
公众号名称/关键词
→ 搜狗微信公开搜索
→ 获取公开文章链接
→ 抓取公开正文
→ 返回统一 JSON
```

新流程不需要微信 Cookie/Token。现有微信后台登录历史链路标记为废弃，接口和代码暂时保留，但不参与新探查、正式抓取和依赖注入；未来需要时另行评估启用。

### 5.3 保存与签名

- `CrawlMethod`、`Source`、`CrawlMethodDomain` 和现有审核字段继续复用。
- `crawl_methods.dsl_recipe` 在迁移期继续作为执行入口，保存规范化插件 Manifest，不再保存新 DSL。
- `signature` 根据规范化 Manifest 与插件内容校验和生成。
- 数据库保存执行入口和元数据，版本目录保存代码实体。
- 路径越界、文件缺失、入口不匹配或校验和不一致时直接拒绝执行。
- 新版本写入新目录，不覆盖旧版本。

## 6. 插件执行协议

插件入口：

```python
async def crawl(request, context) -> dict:
    ...
```

插件返回结构必须与当前 `run_method()` 一致：

```json
{
  "items": [
    {
      "title": "...",
      "url": "https://...",
      "published_at": "2026-08-21T08:00:00Z",
      "summary": "...",
      "content": "..."
    }
  ],
  "stats": {
    "discovered_count": 1
  }
}
```

容器进程协议：

- stdin：主程序传入抓取请求 JSON。
- stdout：只能输出一份最终结果 JSON。
- stderr：运行日志，实时转成结构化事件。
- OpenHands callback 为每个工具开始、完成和错误输出独立 stdio frame；只包含脱敏 action 摘要、参数/结果摘要和域 proposal，不包含 thought、reasoning、原始正文或完整命令。该 stdout 永远标记 `provenance=agent_untrusted`，request ID 只用于 turn 关联而不代表真实性；宿主收到 frame 即持久化审计事件并刷新最新 checkpoint。域授权只认可信 proxy 的独立复查，token usage 只认可信 relay 对 provider response 的计量。
- 退出码：区分成功、插件错误、超时、取消和 Runtime 错误。

正式执行流程：

```text
手动/定时任务
→ 现有 FetchJob 控制层
→ 统一插件执行入口
→ 创建 gVisor 临时容器
→ 只读挂载已审核插件目录
→ 通用 crawler_runner 加载 crawler:crawl
→ 捕获 stdout JSON 与 stderr 日志
→ 校验 items + stats
→ 应用现有抓取限制
→ CrawlOutputIngester + Pipeline
→ 销毁容器
```

现有 `run_killable_fetch()` 继续负责运行记录、取消和结果回传，但不再在子进程中直接解释新 DSL，而是调用 `SandboxRuntime`。取消时按 `run_id` 终止对应容器。

手动抓取与 `morning_crawl` 中重复的“执行、限制、部分成功、入库”逻辑必须收敛到同一个内部入口。

## 7. gVisor 沙箱与资源调度

使用本机 Docker + Google gVisor `runsc`。当前服务器没有 `/dev/kvm`，使用 `systrap` 平台；本轮不引入 Cube 或独立 KVM 节点。

Discovery Agent 与正式抓取使用两个独立版本化 Runtime 镜像。一次 Discovery 保留一个临时 OpenHands gVisor workspace 到首次 Build 及全部 Repair 结束；每次正式 Execute 仍创建独立的 crawler-runtime gVisor 容器。任务结束后全部销毁，不为网站保留常驻进程。

全局规则：

- 最多同时运行 4 个 gVisor 容器。
- 保留现有 Discovery 最多 3 个并发的限制和用户日志隔离。
- 容量满时进入可取消等待队列，不直接失败。
- 优先级：正式定时抓取 > 手动抓取 > Discovery > 自动修复。
- 容器设置 CPU、内存、进程数和墙钟超时限制。

Agent 权限：

- Bash、浏览器和文件工具只能作用于容器内临时 `/workspace`。
- 不挂载项目源码、数据库、Docker Socket、宿主机目录或主程序密钥。
- 插件正式运行时只读挂载自己的制品目录。
- `context` 不提供数据库 Session，插件只能返回候选条目。

网络策略：

- 仅允许访问 Manifest 声明的目标域名、必要 API/CDN 域名。
- 始终禁止宿主机、数据库、内网 IP、环回地址、保留地址和云元数据地址。
- 重定向到未声明域名时拒绝访问，并把候选域名交给 Agent 更新 Manifest 后重新审核。
- 域名白名单由受控出口代理/网络策略执行，不能只依赖插件自觉检查。

## 8. Runtime 依赖管理

固定镜像可以更新，但必须版本化、不可原地覆盖：

```text
Agent 在 Discovery 沙箱试装“包==固定版本”
→ 记录依赖和用途
→ 更新 Runtime 锁定依赖
→ 构建 crawler-runtime:vNext
→ 在新镜像重新执行与验收插件
→ Manifest 固定 runtime_version
→ 审核发布
```

- Discovery 试装依赖也必须指定固定版本。
- 正式抓取不访问 PyPI、不临时安装依赖。
- 旧插件继续引用旧 Runtime；升级前必须重新验证。
- Agent、构建器和审核阶段的职责必须分离，Agent 无权自行发布 Runtime。

## 9. 确定性验收标准

Agent 生成的代码不能以“运行未报错”作为通过条件。普通网站和微信采集器至少满足：

- 至少返回 5 条新闻；低频站点允许人工例外审核。
- 标题有效率 100%。
- URL 为绝对地址且有效率 100%。
- `published_at` 可解析率 100%。
- 每条均有 `content` 或 `summary`。
- 至少 80% 条目的正文/摘要长度达到 200 字符。
- URL 去重率至少 90%，抽样 URL 可访问。
- 声明支持分页时，下一页必须产生新条目。
- 独立试运行连续成功 2 次。
- 通过现有方法审计后，再通过现有质量审计。

验收失败必须把具体字段、样本、异常和工具输出反馈给 Repair 阶段，禁止只给“质量不佳”等模糊结论。

## 10. 检查点、恢复与自动修复

每轮结束保存检查点：

- 当前阶段和轮次。
- 插件草稿与 Manifest 路径。
- RAG 引用、工具证据和结构化处理摘要。
- 执行结果、校验结果、代码 diff 和错误。
- 已用时间、Token 和 Runtime 版本。

后端重启后：

- 终止或清理遗留容器。
- 将未完成运行标记为 `interrupted`。
- 用户可从最近检查点恢复，恢复时创建新容器，不尝试连接旧进程。

正式插件连续 3 次抓取失败后：

1. 自动创建并启动一次修复任务。
2. 同样遵守 30 分钟共享预算、宿主 Repair 上限和沙箱限制。
3. 成功时生成新版本并进入待审核。
4. 失败时保存诊断，停止自动重试。
5. 任何修复版本都不得自动替换生产版本。

## 11. 数据库变更

原计划“首版不改表结构”取消，增加最小必要迁移：

### 扩展 `site_discovery_runs`

- `trigger_type`：manual / repair / resume。
- `phase`：当前 Loop 阶段。
- `round`：当前轮次。
- `checkpoint_path`：最近检查点目录。
- `repair_method_id`：自动修复来源方法。
- `runtime_version`：本次使用的 Runtime。
- 支持 `queued`、`interrupted`、`repairing` 等状态。

### 新增 `discovery_experiences`

保存已审核经验、网站特征、失败修复摘要、Embedding、Embedding 版本和来源方法。

### 新增 `discovery_run_events`

按 `run_id + sequence` 保存阶段变化、行动摘要、工具调用、结果摘要、代码 diff、校验结果和错误，用于 SSE 重连和运行回放。

模型逐 Token 增量、重复心跳和临时 stdout 碎片不落库。敏感字段必须在写日志和写库前统一脱敏。

`CrawlMethod` 继续复用，Runtime 版本保存在 Manifest，不额外建立 Runtime 表。

## 12. API 兼容边界

“保留接口”只表示保持外部 HTTP 契约，不表示保留旧探查或旧查询方法。

保留：

- `POST /discovery/run`
- `POST /discovery/multi-run`
- `GET /discovery/runs`
- `GET /discovery/runs/{run_id}`
- `POST /discovery/runs/{run_id}/cancel`
- 方法列表、详情、审核、删除、启用/禁用和提醒接口
- `POST /discovery/methods/{id}/fetch` 及取消接口

普通网站和微信请求进入新的 Loop Engine；内部论坛仍停留在保留分流；旧 Explorer、Validator、DSL Writer、Auditor 及网站/微信 DSL 解释逻辑不再作为新接口的内部实现。

兼容新增：

- `POST /discovery/runs/{run_id}/resume`
- `GET /discovery/runs/{run_id}/events`：由前端通过 `fetch` 携带认证 Header 读取的 SSE 流
- 运行查询增加 `phase`、`round`、`queue_position`、`runtime_version` 等可选字段
- 状态增加 `queued`、`interrupted`、`repairing`

原有字段不删除、不改名。正式抓取继续返回当前的 `run_id`、`discovered_count`、`stored_count`、`items`、`stats` 和 `message`。

## 13. SSE 日志与可审计 Agent 过程

当前 `/run-logs` 每 1.5 秒轮询会造成日志成批出现和前端跳动。Discovery 改用 SSE：后端产生事件后立即推送，连接断开后通过事件序号续传。

允许展示：

- 当前目标和阶段。
- Agent 的行动与修改理由摘要。
- 工具调用参数的脱敏摘要、开始/结束和观察结果。
- 代码 diff、gVisor 运行状态和确定性校验结果。
- 重试原因、轮次、剩余时间和最终结论。

不展示模型私有完整思维链；使用可审计的工作轨迹：

```text
目标 → 行动 → 工具结果 → 判断摘要 → 修改
```

前端规则：

- 日志按时间从上到下追加，不再倒序插入。
- 事件先进入缓冲区，每 50～100ms 批量渲染。
- 使用虚拟列表，只渲染可见日志。
- 用户向上查看历史时暂停自动滚动。
- 断线自动续传；模型网关不支持 Token 流时，仍持续推送阶段、工具和校验事件。
- 继续保证不同用户、不同 `run_id` 的日志互不混合。

## 14. 前端流程图重设计

删除当前“Explorer / Validator / DSL Writer / Auditor 四 Agent 环”的表达，改成与单 Agent Loop 一致的流程：

```text
[准备上下文 / RAG]
          ↓
┌────── 第 N 轮（宿主有界）──────┐
│ 探查 → 编写 → 沙箱执行 → 校验 │
│   ↑          失败 → 修复 ───┘ │
└───────────────────────────────┘
          ↓ 通过
[封装制品] → [进入待审核]
```

- 流程图直接读取后端 `phase` 和 `round`，不再通过 `node_trace` 猜测当前节点。
- 显示排队位置、已用/剩余时间、网站/微信类型和 Runtime 版本。
- 支持 `queued`、`interrupted`、`repairing`、`failed`、`completed`。
- 点击阶段查看对应证据、代码变化、执行输出和校验结果。
- 圆形不再表示多个 Agent，界面明确说明“一个 Agent，在受控 Loop 中工作”。

## 15. 旧 DSL 全量迁移

目标不是长期兼容旧 DSL，而是最终全部替换：

```text
旧 DSL 继续正式运行
→ 新 Agent 生成 Python 插件
→ 新旧方式逐站双跑并对比
→ 新插件连续通过验收
→ 逐站切换到插件
→ 进入回滚观察期
→ 全站完成后删除网站/微信旧 DSL 制品与解释逻辑
```

单站切换条件：

- 新插件满足全部确定性验收标准。
- 新旧结果完成对比，没有不可解释的大量缺失或错误。
- 新插件已审核批准。

旧 DSL 在切换后至少保留 **7 天且新插件完成 3 次正式抓取成功**；两个条件同时满足后才删除。回滚期内插件异常可立即切回旧 DSL。

迁移结束后，普通网站和微信不再生成或执行 DSL。删除共享解释器前必须先拆出内部论坛所需的最小兼容入口，确保其现有接口和分流不受影响。

## 16. 替换与复用清单

| 范围 | 处理方式 |
|---|---|
| 普通网站固定动作 Explorer、JSON 源码 Build、Validator、DSL Writer、Auditor、LangGraph | 固定动作路径冻结且不再被新流程调用；替换为 WebsiteLoopEngine + OpenHands SDK 单 Agent 会话 |
| 网站/微信新 DSL 生成与解释执行 | 替换为 Python 插件 + gVisor Runtime |
| 网站/微信旧 DSL 制品与解释逻辑 | 仅迁移回滚期临时保留，最终删除 |
| Discovery HTTP 路径与原有返回字段 | 保留外部契约，内部实现替换 |
| `SiteDiscoveryRun`、并发限制、用户日志隔离、Token 统计 | 复用并扩展 |
| `CrawlMethod`、`Source`、`CrawlMethodDomain`、签名去重 | 复用 |
| 待审核、批准、删除、启用/禁用、提醒 | 复用 |
| 方法审计、质量审计 | 复用接口，输入改为插件真实试运行结果 |
| 手动/定时抓取、取消、部分成功、统计 | 复用并收敛到统一内部执行入口 |
| `CrawlOutputIngester` 与新闻 Pipeline | 原样复用 |
| 微信后台登录历史链路 | 标记废弃，代码与接口暂留，不主动使用 |
| 内部论坛 | 只保留接口和分流，本轮不实现 |
| `agent_crawl` | 完全排除 |

## 17. 后续研究项（本轮不实现）

以下能力只做备案，不进入本轮交付：

- 验证码自动处理。
- 浏览器指纹兼容。
- 代理网络能力。

任何实现前必须单独完成合规、安全和目标网站授权评估。当前只使用正常 HTTP/浏览器访问、合理请求频率、缓存、有限重试、随机退避和公开入口；仍遇验证码或限流时返回部分结果并标记 `captcha_required` / `rate_limited`。

## 18. 实施顺序

1. 定义插件、Manifest、Runner、统一错误码和 stdout/stderr 契约。
2. 建立 gVisor `SandboxRuntime`、固定 Runtime 镜像、网络策略和全局容量队列。
3. 完成数据库迁移、检查点、事件流和恢复机制。
4. 实现轻量 RAG 与 WebsiteLoopEngine，并以 OpenHands SDK 的保留式 gVisor 会话替换普通网站 Explore + Build + Repair。
5. 实现共享无登录搜狗微信采集器，保留内部论坛接口。
6. 接入现有方法审计、质量审计、待审核和版本制品管理。
7. 将手动抓取与 `morning_crawl` 收敛到统一插件执行入口。
8. 增加 SSE 日志并重设计前端单 Agent Loop 流程图。
9. 按站点逐一双跑迁移旧 DSL，满足回滚条件后删除旧制品。
10. 全量迁移完成后删除网站/微信旧 DSL 生成与解释执行代码。

## 19. 必须保持不变

- 不修改或复用 `agent_crawl`。
- 不破坏现有 Discovery 外部 HTTP 调用和正式抓取返回结构。
- 不绕过待审核流程，不通过的插件不能参与正式抓取。
- 插件不得直接读写数据库或访问主程序密钥。
- 新闻仍由现有 Ingester 和 Pipeline 完成规范化、去重、富化和入库。
- 单个插件失败不能影响其他方式，已取得的合法部分结果继续保留。
- 正式插件执行不调用 Agent、RAG 或 LLM。
