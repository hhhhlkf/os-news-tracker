# OS News Tracker

群组化技术新闻情报平台，自动从多类来源采集 OS 技术情报，用 LLM 整理归纳，汇总到支持个性化推荐的 React 网页。支持群组订阅、AI 智能爬取、个性化评分、趋势总结和实时日志。

## 版本状态

### V1（已完成）

- 从 4 类数据源采集（RSS、结构化 API、页面监控、关键词搜索）
- 两条数据处理流：
  - **新闻动态流** — LLM 结构化摘要（分类、子标签、实体、摘要）
  - **结构化事实流** — 安全公告/CVE、生命周期/EOL → 直接解析入库
- 统一流水线：`fetch → normalize → [relevance-filter] → dedup → enrich → store`
- 可查询 Web UI：搜索、分面筛选、分页、条目详情侧滑面板、手动新闻运行控制
- Docker Compose 一键部署

### V2（开发中）

| 功能 | 说明 |
|------|------|
| 用户账户系统 | 邮箱注册登录，JWT 认证，subscriber / system_admin 角色 |
| AI 智能爬取引擎 | Handoff Chain（PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool），质量评估 + SiteMemory |
| 群组订阅体系 | 三级权限（system_admin / group_admin / subscriber），群组共享爬取成本，群级内容过滤 |
| 个性化推荐评分 | 用户自定义 Scoring Criteria，0-100 相关度分，快速通道 + LLM 精打分 |
| 趋势总结（Digest） | DigestAgent 多步 Chain，热点识别 + 新兴趋势，按群定时生成 |
| 实时日志面板 | SSE + 环形缓冲，JetBrains Mono 风格，覆盖所有后端进程 |

### 当前已落地的控制台能力

- **首页新闻流**
  - 支持搜索、主分类/信息类型/重要度筛选、时间窗口筛选、条目详情查看
  - 已接入 **邮件任务中心** 与 **系统晨抓配置与执行窗口**
- **智能探查 / 发现工作台**（`/discover`）
  - 智能探查：把网站、微信搜索、公众号历史等输入路由成对应 discovery recipe
  - 抓取模块 · 爬取方式库：批量复用已保存的 crawl methods 执行抓取
  - 抓取模块 · Prompt 工作室：管理 discovery / enrich 相关 prompt 套餐，支持启用、停用、自定义覆盖
  - 抓取模块 · 主分类修改：维护富集阶段可用主分类，并同步影响条目分类
- **邮件任务中心**
  - 支持基于当前首页筛选条件生成 HTML 邮件预览
  - 支持立即发送、模板保存、模板列表、预定发送列表
  - 邮件发送通道同时支持 `TOF4 API` 与 `SMTP`
- **系统晨抓**
  - 只面向 `抓取模块 · 爬取方式库` 中的 active discovery methods
  - 支持晨抓配置、立即执行、停止执行、今日状态、运行时间线、失败方式明细
  - 调度与巡检都会写入统一日志流

详细设计文档：
- V2 综合设计：[`docs/superpowers/specs/2026-06-15-v2-complete-design.md`](docs/superpowers/specs/2026-06-15-v2-complete-design.md)
- 群组订阅设计：[`docs/superpowers/specs/2026-06-15-group-subscription-design.md`](docs/superpowers/specs/2026-06-15-group-subscription-design.md)
- 管理层实施计划：[`docs/superpowers/plans/2026-06-15-v2-implementation-schedule.md`](docs/superpowers/plans/2026-06-15-v2-implementation-schedule.md)

## 快速开始

从零开始部署本项目。**推荐使用 Docker Compose（方式一）**，无需手动安装 Python 或 Node.js。

### 方式一：Docker Compose 部署（推荐）

适用于部署和生产环境。只需 Docker，其他依赖由容器自动处理。

#### 1. 环境准备

##### macOS

安装 [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/)（支持 Intel 和 Apple Silicon）：

```bash
# 方法 A：Homebrew 安装（推荐）
brew install --cask docker

# 方法 B：手动安装
# 从 https://www.docker.com/products/docker-desktop/ 下载 .dmg，拖入 Applications 并启动
```

启动 Docker Desktop 后，打开终端验证：

```bash
docker --version          # 应 >= 24.x
docker compose version    # 应 >= 2.x
```

> **Apple Silicon (M1/M2/M3/M4) 注意事项：** Docker Desktop 默认通过 Rosetta 2 兼容 x86 镜像，本项目使用的基础镜像（python:3.11-slim、node:20-alpine、nginx:alpine、postgres:16）均提供 ARM64 原生支持，无需额外配置。

##### Linux

找一台可以访问外网的 Linux 服务器或虚拟机（CentOS 7+/Ubuntu 20.04+/Debian 11+），然后安装 Docker：

```bash
# === CentOS 7/8/9 ===
sudo yum install -y yum-utils
sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable docker --now

# === Ubuntu / Debian ===
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable docker --now

# === 验证安装 ===
docker --version          # 应 >= 24.x
docker compose version    # 应 >= 2.x
```

#### 2. 克隆仓库

```bash
git clone https://your-git-server/os-news-tracker.git
cd os-news-tracker
```

#### 3. 配置大模型 API（必做）

```bash
# 从模板创建 .env 文件
cp .env.example .env

# 编辑 .env，填入实际的大模型 API 信息（详见下方"大模型 API 配置"章节）
vi .env
```

**最低配置**（系统启动至少需要这几行）：

```ini
LLM_BASE_URL=https://your-llm-gateway.example.com/v1
LLM_API_KEY=sk-your-api-key-here
LLM_MODEL=gpt-4o
DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/osnews
```

#### 4. 启动服务

```bash
docker compose up --build -d
```

首次启动会拉取基础镜像（python:3.11-slim、node:20-alpine、nginx:alpine、postgres:16）并编译前后端，耗时约 3-8 分钟（视网络速度而定）。

#### 5. 验证运行

```bash
# 查看服务状态，3 个服务均为 Up/healthy 即为正常
docker compose ps

# 查看后端日志
docker compose logs backend | tail -20

# 测试 API
curl http://localhost:8000/items
```

#### 6. 访问

| 服务 | 地址 |
| --- | --- |
| 前端页面 | http://localhost:8080 |
| 后端 API | http://localhost:8000 |
| API 交互文档（Swagger） | http://localhost:8000/docs |

#### 常用操作

```bash
docker compose up --build -d   # 启动（后台运行）
docker compose logs -f         # 实时查看所有日志
docker compose logs backend    # 只看后端日志
docker compose restart backend # 重启后端
docker compose down            # 停止所有服务
docker compose down -v         # 停止并删除数据库数据（重置）
```

### 方式二：本地开发环境

适用于二次开发和调试，需手动安装 Python 和 Node.js。

#### 1. 安装运行环境

| 依赖 | 版本要求 | macOS 安装 | Linux 安装 |
| --- | --- | --- | --- |
| Python | 3.11+ | `brew install python@3.11` | `yum install python3.11` / `apt install python3.11` |
| Node.js | 20+ | `brew install node@20` 或 `fnm install 20` | `nvm install 20` 或包管理器 |
| PostgreSQL | 16（可选） | 开发时默认使用 SQLite，无需安装 | 同左 |

```bash
# 验证安装
python3 --version   # 应 >= 3.11（macOS 下命令为 python3）
node --version      # 应 >= 20
```

#### 2. 克隆并安装依赖

```bash
git clone https://your-git-server/os-news-tracker.git
cd os-news-tracker

# 后端
cd backend
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
pip install -e ".[dev]"

# 前端
cd ../frontend
npm ci
```

#### 3. 配置大模型 API

在项目根目录创建 `.env` 文件，填入 LLM 配置（参见下方"大模型 API 配置"章节）：

```bash
cd ..   # 回到项目根目录
cp .env.example .env
vi .env
```

#### 4. 启动

```bash
# 终端 1：启动后端（默认使用 SQLite）
cd backend && source .venv/bin/activate
ENABLE_SCHEDULER=0 uvicorn app.entry:app --reload --port 8000

# 终端 2：启动前端
cd frontend
npm run dev
```

- 后端 API: http://localhost:8000
- 前端热更新: http://localhost:5173

---

## 技术栈

| 层 | 技术 |
|---|------|
| 后端 | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, PostgreSQL, APScheduler |
| 认证（V2）| python-jose[cryptography]（JWT），passlib[bcrypt] |
| 前端 | React 19, Vite, TypeScript, TanStack Query, react-router-dom（V2）|
| 采集 | feedparser, Scrapling, httpx |
| AI/LLM | 可配置的 OpenAI 兼容 client（司内 LLM 网关） |
| 测试 | pytest, pytest-asyncio, respx, vitest |
| 部署 | Docker Compose |

## 项目结构

```
os-news-tracker/
├── README.md
├── CLAUDE.md
├── .env.example
├── docker-compose.yml
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── app/
│   │   ├── config.py             # 配置（pydantic-settings，环境变量驱动）
│   │   ├── db.py                 # SQLAlchemy engine + Session
│   │   ├── models.py             # ORM 模型
│   │   ├── entry.py              # 应用入口
│   │   ├── pipeline.py           # 新闻流编排
│   │   ├── scheduler.py          # APScheduler 定时任务
│   │   ├── repository.py         # 数据库读写查询
│   │   ├── api/                  # FastAPI 路由
│   │   ├── sources/              # 数据源注册 + 种子数据
│   │   ├── fetchers/             # 采集器（RSS/页面监控/搜索）
│   │   ├── extract/              # 内容提取（Scrapling）
│   │   ├── search/               # 搜索提供者（可插拔）
│   │   ├── processing/           # 标准化/去重/相关性过滤/LLM富化
│   │   └── llm/                  # OpenAI 兼容 LLM client
│   └── tests/
├── frontend/
│   ├── Dockerfile
│   ├── nginx.conf
│   └── src/
│       ├── App.tsx
│       ├── api/client.ts         # API 客户端（TanStack Query）
│       └── components/           # UI 组件
└── docs/
    └── superpowers/
        ├── specs/                # 设计文档
        └── plans/                # 实现计划
```

## 本地开发

### 环境要求

- Python 3.11+
- Node.js 20+
- PostgreSQL（可选，默认使用 SQLite 内存数据库进行开发）

### 后端

```bash
cd backend

# 安装依赖（含开发依赖）
pip install -e ".[dev]"

# 运行测试
ENABLE_SCHEDULER=0 python -m pytest tests/ -v

# 启动开发服务器
uvicorn app.entry:app --reload --port 8000
```

### 前端

```bash
cd frontend

# 安装依赖
npm ci

# 启动开发服务器（热更新）
npm run dev

# 类型检查 + 构建
npm run build
```

### 数据库迁移

```bash
cd backend

# 生成新迁移
alembic revision --autogenerate -m "描述"

# 运行迁移
alembic upgrade head
```

## Docker Compose 部署

```bash
# 1. 创建 .env 文件（参考 .env.example）
cp .env.example .env
# 编辑 .env，填入实际的 LLM_BASE_URL、LLM_API_KEY 等

# 2. 启动所有服务
docker compose up --build

# 3. 访问
# 前端: http://localhost:8080
# 后端 API: http://localhost:8000
# API 文档: http://localhost:8000/docs
```

服务包括：
- **db** — PostgreSQL 16
- **backend** — FastAPI（端口 8000）
- **frontend** — Nginx + React 静态文件（端口 8080）

## 环境变量

| 变量 | 默认值（开发） | Docker/生产推荐值 | 说明 |
| --- | --- | --- | --- |
| `DATABASE_URL` | `sqlite+pysqlite:///:memory:` | `postgresql+psycopg://postgres:postgres@db:5432/osnews` | 数据库连接串 |
| `LLM_BASE_URL` | `http://llm.invalid/v1` | 司内 LLM 网关地址 | LLM 网关地址（OpenAI 兼容 API） |
| `LLM_API_KEY` | `test-key` | 司内网关 API 密钥 | LLM 网关 API 密钥 |
| `LLM_MODEL` | `test-model` | 司内默认模型名 | 使用的模型名称 |
| `LLM_MAX_CONCURRENCY` | `4` | `4` | LLM 请求最大并发数 |
| `SEARCH_PROVIDER` | `none` | `internal` | 搜索提供者（可选值：`none`、`internal`） |
| `FETCH_USER_AGENT` | `os-news-tracker/0.1 (+internal)` | 同左 | HTTP 请求 User-Agent |
| `FETCH_PER_HOST_DELAY_SECONDS` | `2.0` | `2.0` | 同主机请求间隔（秒） |
| `ENABLE_SCHEDULER` | `1` | `1` | 是否启用定时任务调度（测试时设为 `0`） |
| `JWT_SECRET_KEY` | `change-me-in-production-use-32+-chars` | 32 位以上随机字符串 | JWT 签名密钥（V2，必须修改）|
| `WECHAT_MP_COOKIE` | 空 | 实际公众号平台 cookie | 微信公众号历史抓取鉴权 |
| `WECHAT_MP_TOKEN` | 空 | 实际公众号平台 token | 微信公众号历史抓取鉴权 |
| `MAIL_PROVIDER` | `tof4` | `tof4` / `smtp` | 默认邮件发送通道 |
| `TOF4_PAASID` | 空 | 司内 TOF4 应用 ID | TOF4 邮件通道配置 |
| `TOF4_TOKEN` | 空 | 司内 TOF4 token | TOF4 邮件通道配置 |
| `TOF4_URL` | 空 | 实际 TOF4 发送地址 | TOF4 邮件通道配置 |
| `SMTP_HOST` | `localhost` | 企业 SMTP 主机 | SMTP 发信主机 |
| `SMTP_PORT` | `25` | `465` / `587` 等 | SMTP 发信端口 |
| `SMTP_USERNAME` | 空 | 发件账号 | SMTP 登录用户名 |
| `SMTP_PASSWORD` | 空 | SMTP 授权码/密码 | SMTP 登录密码 |
| `SMTP_FROM_EMAIL` | `no-reply@example.com` | 实际发件邮箱 | SMTP 默认发件人 |
| `SMTP_FROM_NAME` | `OS News Tracker` | 实际显示名 | SMTP 默认发件名 |
| `SMTP_USE_TLS` | `0` | `0` / `1` | SMTP STARTTLS 开关 |
| `SMTP_USE_SSL` | `0` | `0` / `1` | SMTP SSL 开关 |

配置使用 pydantic-settings，自动从 `.env` 文件加载。

### 邮件与微信相关示例

`.env.example` 已内置两套邮件通道与微信抓取配置骨架：

- **微信历史抓取**
  - `WECHAT_MP_COOKIE`
  - `WECHAT_MP_TOKEN`
- **TOF4 邮件发送**
  - `MAIL_PROVIDER=tof4`
  - `TOF4_PAASID` / `TOF4_TOKEN` / `TOF4_URL`
- **SMTP 邮件发送**
  - `MAIL_PROVIDER=smtp`
  - `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD`

如果只做本地开发而不测试邮件或公众号历史抓取，这些字段可以先留空。

## 大模型 API 配置

系统通过 OpenAI 兼容协议接入大模型，用于新闻分类、标签提取、实体识别和摘要生成。**启动前必须正确配置以下 3 个环境变量**。

### 支持的大模型服务

任何兼容 OpenAI `/v1/chat/completions` 接口的服务均可使用：

| 服务商 | `LLM_BASE_URL` 示例 | 获取 API Key |
| --- | --- | --- |
| **司内 LLM 网关** | `https://llm-gateway.your-company.com/v1` | 联系内部平台团队 |
| **OpenAI** | `https://api.openai.com/v1` | <https://platform.openai.com/api-keys> |
| **Azure OpenAI** | `https://YOUR_RESOURCE.openai.azure.com/openai/deployments/YOUR_DEPLOYMENT` | Azure Portal → OpenAI 资源 |
| **DeepSeek** | `https://api.deepseek.com/v1` | <https://platform.deepseek.com/api_keys> |
| **智谱 GLM** | `https://open.bigmodel.cn/api/paas/v4` | <https://open.bigmodel.cn/usercenter/apikeys> |
| **通义千问** | `https://dashscope.aliyuncs.com/compatible-mode/v1` | <https://dashscope.console.aliyun.com/apiKey> |
| **Moonshot (Kimi)** | `https://api.moonshot.cn/v1` | <https://platform.moonshot.cn/console/api-keys> |
| **Ollama（本地）** | `http://localhost:11434/v1` | 无需（本地运行，可填写任意值） |

### 配置步骤

#### 1. 创建 .env 文件

```bash
cp .env.example .env
```

#### 2. 编辑 .env，填入大模型配置

```bash
# === 必填：大模型 API 配置 ===
# API 地址（OpenAI 兼容格式）
LLM_BASE_URL=https://api.openai.com/v1

# API 密钥
LLM_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 模型名称（按实际服务支持的模型填写）
LLM_MODEL=gpt-4o

# === 可选：大模型调优 ===
# 最大并发请求数，控制同时发往 LLM 的请求量
LLM_MAX_CONCURRENCY=4
```

#### 3. 常见配置示例

**OpenAI：**

```ini
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_MODEL=gpt-4o
```

**司内 LLM 网关：**

```ini
LLM_BASE_URL=https://llm-gateway.your-company.com/v1
LLM_API_KEY=your-internal-api-key
LLM_MODEL=internal-default
```

**Azure OpenAI：**

```ini
LLM_BASE_URL=https://your-resource.openai.azure.com/openai/deployments/gpt-4o
LLM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_MODEL=gpt-4o
# Azure 需要额外的 headers，通过查询参数传递 api-version
# LLM_BASE_URL 末尾追加：?api-version=2024-02-15-preview
```

**DeepSeek：**

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_MODEL=deepseek-chat
```

**Ollama 本地模型：**

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:7b
```

#### 4. 验证配置

```bash
# 用 curl 测试 LLM 连通性
curl -sS "$LLM_BASE_URL/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -d '{"model":"'"$LLM_MODEL"'","messages":[{"role":"user","content":"say hello"}]}' \
  | head -c 500
```

### 模型选择建议

| 用途 | 推荐模型 | 说明 |
| --- | --- | --- |
| 新闻分类 & 标签提取 | GPT-4o / Claude 3.5 Sonnet / Qwen2.5-72B | 分类准确率高，输出格式稳定 |
| 摘要生成 | GPT-4o-mini / DeepSeek-V3 / Qwen2.5-32B | 性价比高，摘要任务足够 |
| 实体提取 | GPT-4o / DeepSeek-R1 | 需要较强的推理能力 |
| 低成本方案 | DeepSeek-V3 / Qwen2.5-32B / GLM-4-Flash | 大部分任务可用，成本大幅降低 |

## 待完成事项

以下功能已预留接口，但需要根据实际环境配置方可使用：

1. **司内 LLM 网关** — 当前 `LLM_BASE_URL` 指向占位地址。部署前必须配置为组织内部的 LLM 网关地址及对应的 `LLM_API_KEY`。LLM client 基于 OpenAI 兼容协议，任何兼容该协议的服务均可接入。

2. **搜索提供者** — `search` 类型的采集器需要一个真实的搜索后端。当前 `SEARCH_PROVIDER` 默认为 `none`（跳过搜索采集）。如需启用，请实现 `SearchProvider` 协议（参见 `backend/app/search/base.py`）并设置 `SEARCH_PROVIDER=internal`。

3. **Firecrawl 内容提取** — 内容提取层基于可插拔的 `ContentExtractor` 协议设计。默认使用 Scrapling。如需更高精度的提取（尤其是 JS 渲染页面），可接入 Firecrawl 作为替代提取器，但需注意其 AGPL 许可证的影响。

4. **Alembic 生产迁移** — 当前开发模式下使用 `Base.metadata.create_all()` 在启动时自动建表。生产环境应使用 Alembic 迁移管理数据库 schema 变更。

## Discovery 与晨抓说明

### Discovery（智能探查 + 爬取方式库）

- discovery 负责把输入沉淀为可复用的 `crawl method`
- 当前支持的网站路径、微信搜索路径、微信公众号历史路径等多源 recipe
- `Prompt 工作室` 管 discovery 与 enrich 的 prompt 套餐，不会直接改动代码默认模板
- `主分类修改` 用于维护 enrich 阶段的主分类池

### 系统晨抓

- 晨抓只使用 **discovery methods**
- 不再以旧 `/sources` 业务模型作为晨抓主入口
- 每次晨抓会根据配置生成抓取请求，执行 active methods，并记录：
  - 今日状态
  - 最近运行记录
  - 方法级成功/失败明细
  - 日志时间线
