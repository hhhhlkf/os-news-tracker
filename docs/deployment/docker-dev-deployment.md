# OS News Tracker Docker 开发部署说明

本文档对应当前项目实际使用的开发环境：数据库、后端、前端都运行在 Docker 容器中，并且前后端代码修改后支持热更新。

适用编排文件：

- `docker-compose.dev.yml`

适用场景：

- 本机开发
- 迁移到一台已安装 Docker 的 Linux 服务器后继续以开发模式运行
- 保持数据库也在容器中，同时保留前端和后端热更新能力

## 1. 环境目标

这套开发环境包含 3 个容器：

- `db`：PostgreSQL 16
- `backend`：FastAPI 后端，使用 `uvicorn --reload`
- `frontend`：Vite 开发服务器，使用 `npm run dev`

当前对外端口：

- 前端：`5173`
- 后端：`8000`
- 数据库：`15432`

## 2. 热更新机制

### 后端热更新

后端在 `docker-compose.dev.yml` 中挂载了本地源码目录：

```yaml
volumes:
  - ./backend:/app
command: uvicorn app.entry:app --host 0.0.0.0 --port 8000 --reload
```

因此修改 `backend/` 下代码后，容器内 `uvicorn` 会自动重载。

### 前端热更新

前端在 `docker-compose.dev.yml` 中挂载了本地源码目录：

```yaml
volumes:
  - ./frontend:/app
  - frontend_node_modules:/app/node_modules
command: npm run dev -- --host 0.0.0.0 --port 5173
```

因此修改 `frontend/` 下代码后，Vite 会自动热更新页面。

说明：

- `frontend_node_modules` 使用独立 volume，避免宿主机目录覆盖容器内依赖
- 数据库不需要热更新，数据通过 Docker volume 持久化

## 3. 前置要求

目标机器需要已经安装：

- Docker
- Docker Compose Plugin

建议验证：

```bash
docker --version
docker compose version
```

## 4. 项目文件准备

将整个项目目录放到目标机器，例如：

```bash
git clone <your-repo-url> os-news-tracker
cd os-news-tracker
```

如果不是通过 Git 拉取，也可以直接复制整个项目目录到服务器。

## 5. 环境变量配置

在项目根目录准备 `.env` 文件。

如果首次部署：

```bash
cp .env.example .env
```

然后编辑 `.env`，至少保证以下配置存在：

```ini
POSTGRES_DB=osnews
POSTGRES_USER=osnews_app
POSTGRES_PASSWORD=change-me
DATABASE_URL=postgresql+psycopg://osnews_app:change-me@db:5432/osnews
LLM_BASE_URL=https://your-llm-gateway.example.com/v1
LLM_API_KEY=sk-your-api-key
LLM_MODEL=your-model
LLM_MAX_CONCURRENCY=4
SEARCH_PROVIDER=internal
FETCH_USER_AGENT=os-news-tracker/0.1 (+internal)
FETCH_PER_HOST_DELAY_SECONDS=2
```

说明：

- `DATABASE_URL` 中主机名必须保持为 `db`
- 因为后端运行在 Docker Compose 网络中，`db` 是数据库容器服务名
- 当前开发环境数据库名使用 `osnews`

## 6. 启动开发环境

在项目根目录运行：

```bash
docker compose -f docker-compose.dev.yml up --build -d
```

首次启动会完成以下事情：

- 构建后端开发镜像
- 构建前端开发镜像
- 启动 PostgreSQL 容器
- 启动 FastAPI 开发服务
- 启动 Vite 开发服务

查看状态：

```bash
docker compose -f docker-compose.dev.yml ps
```

正常情况下应看到：

- `db` 为 `healthy`
- `backend` 为 `Up`
- `frontend` 为 `Up`

## 7. 访问地址

启动后可通过以下地址访问：

- 前端页面：`http://<server-ip>:5173`
- 后端 API：`http://<server-ip>:8000`
- Swagger 文档：`http://<server-ip>:8000/docs`

如果在本机运行，也可以直接访问：

- `http://localhost:5173`
- `http://localhost:8000`

## 8. 常用运维命令

### 查看全部服务状态

```bash
docker compose -f docker-compose.dev.yml ps
```

### 查看全部日志

```bash
docker compose -f docker-compose.dev.yml logs -f
```

### 查看后端日志

```bash
docker compose -f docker-compose.dev.yml logs -f backend
```

### 查看前端日志

```bash
docker compose -f docker-compose.dev.yml logs -f frontend
```

### 查看数据库日志

```bash
docker compose -f docker-compose.dev.yml logs -f db
```

### 重启后端

```bash
docker compose -f docker-compose.dev.yml restart backend
```

### 重启前端

```bash
docker compose -f docker-compose.dev.yml restart frontend
```

### 停止服务

```bash
docker compose -f docker-compose.dev.yml down
```

### 停止并删除数据卷

```bash
docker compose -f docker-compose.dev.yml down -v
```

注意：

- `down -v` 会删除开发数据库数据
- 只有确认不再需要当前数据库内容时再执行

## 9. 数据持久化说明

开发环境数据库数据保存在 Docker volume：

- `pgdata_dev`

这意味着：

- 即使重启 `backend` 或 `frontend`，数据库数据仍会保留
- 执行 `docker compose -f docker-compose.dev.yml down` 不会删除数据库数据
- 只有执行 `down -v`，或者手动删除 volume，数据库数据才会丢失

查看 volume：

```bash
docker volume ls | grep pgdata_dev
```

## 10. 数据库导入导出说明

项目根目录下的 `db/` 目录可用于保存数据库导出文件。

例如当前可以保存：

- `db/osnews_empty_test_schema.sql`
- `db/osnews_empty_test_full.dump`

### 导出完整数据库

```bash
mkdir -p db

docker compose -f docker-compose.dev.yml exec -T db \
  pg_dump -U postgres -d osnews_empty_test -Fc \
  > db/osnews_empty_test_full.dump
```

### 导出表结构

```bash
docker compose -f docker-compose.dev.yml exec -T db \
  pg_dump -U postgres -d osnews_empty_test --schema-only \
  > db/osnews_empty_test_schema.sql
```

### 恢复数据库

先启动数据库容器：

```bash
docker compose -f docker-compose.dev.yml up -d db
```

然后重建数据库：

```bash
docker compose -f docker-compose.dev.yml exec -T db \
  psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS osnews_empty_test WITH (FORCE);"

docker compose -f docker-compose.dev.yml exec -T db \
  psql -U postgres -d postgres -c "CREATE DATABASE osnews_empty_test;"
```

再恢复数据：

```bash
DB_CONTAINER_ID="$(docker compose -f docker-compose.dev.yml ps -q db)"

docker cp db/osnews_empty_test_full.dump "${DB_CONTAINER_ID}:/tmp/osnews_empty_test_full.dump"

docker compose -f docker-compose.dev.yml exec -T db \
  pg_restore -U postgres -d osnews_empty_test --clean --if-exists --no-owner --no-privileges /tmp/osnews_empty_test_full.dump

docker compose -f docker-compose.dev.yml exec -T db rm -f /tmp/osnews_empty_test_full.dump
```

## 11. 常见问题

### 1. 修改后端代码后没有自动重载

先看后端日志：

```bash
docker compose -f docker-compose.dev.yml logs -f backend
```

确认：

- 修改的是 `backend/` 目录内文件
- 当前启动方式确实是 `docker-compose.dev.yml`
- `backend` 容器命令仍是 `uvicorn ... --reload`

### 2. 修改前端代码后页面没有热更新

先看前端日志：

```bash
docker compose -f docker-compose.dev.yml logs -f frontend
```

确认：

- 修改的是 `frontend/` 目录内文件
- `frontend` 服务正常启动的是 Vite dev server
- 浏览器访问的是 `5173` 端口，而不是生产静态站点端口

### 3. 数据库连接失败

重点检查：

- `.env` 中 `DATABASE_URL` 是否仍然写的是 `@db:5432`
- `db` 服务是否已经 `healthy`
- 是否误删了数据库 volume

### 4. 重建容器后数据丢失

通常原因有两个：

- 执行了 `docker compose ... down -v`
- 手动删除了 `pgdata_dev` volume

如果之前已经导出到 `db/` 目录，可以按上面的恢复流程重新导入。

## 12. 推荐使用方式

如果你当前目标是：

- 整个项目跑在 Docker 中
- 数据库也在 Docker 中
- 前后端保留热更新

那么建议统一使用：

```bash
docker compose -f docker-compose.dev.yml up --build -d
```

不要混用：

- `docker-compose.yml` 的生产式启动方式
- 宿主机本地 Python/Node 直跑方式

这样可以减少环境差异，也更适合迁移到新的 Linux 服务器后继续开发和调试。
