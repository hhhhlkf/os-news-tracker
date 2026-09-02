# OS News Tracker 部署运行手册

本文档记录 2026-09-02 已核对的容器拓扑，以及开发、发布、回滚和配置重载的操作边界。它是部署操作的权威说明；`README.md` 提供摘要，`AGENTS.md` 和 `CLAUDE.md` 提供给自动化代理的约束。

## 运行拓扑（已核对）

| 环境 | 操作目录 / Compose project | 当前状态 | 对外入口 | 数据与运行边界 |
| --- | --- | --- | --- | --- |
| 开发 | `/data/workspace/os-news-tracker-dev` / `os-news-tracker-dev` | `db` 运行；`backend`、`frontend` 当前停止 | 启动后：`8000`、`5174` | 源码 bind mount；全部 scheduler 强制关闭；数据库卷为外部 `os-news-tracker_pgdata` |
| 正式 | `/data/workspace/os-news-tracker` / `os-news-tracker-prod-86e2c28` | `backend-prod`、`frontend-prod` 运行 | `80` | 无应用源码热更新；经 `os-news-tracker_default` 网络访问 `db`；当前数据库容器的卷为 `os-news-tracker_pgdata_dev` |
| 上一正式版本 | `os-news-tracker-prod-3180205` | 前后端均停止，保留作立即回滚 | 无 | 不要删除，直到下一版本健康检查通过且其成为“更早”版本 |

不要用卷名称猜测环境或数据是否共用。必须同时核对 Compose project、工作目录、`DATABASE_URL` 主机、网络和容器挂载。正式 Compose 不会创建数据库；它使用外部网络上名为 `db` 的服务。

正式 backend 没有将应用源码挂进 `/app`，因此代码是镜像化的；它会挂载 `/data/workspace/os-news-tracker/backend/connectors/sites` 和 WeChat、attestation、checkpoint 数据卷。正式 frontend 单独挂载 Nginx 配置。发布不得删除、清空或替换这些运行数据。

## 通用规则

- 根目录 `docker-compose.yml` 已退休。禁止执行裸 `docker compose`。
- 开发只使用 `docker compose -f docker-compose.dev.yml ...`。
- 正式只使用 `docker compose -p <release-project> -f docker-compose.production.yml ...`，且从 `/data/workspace/os-news-tracker` 执行。
- `.env` 包含密钥、Cookie、Token 和邮件凭据。不得提交、打印或复制到文档、终端记录和镜像层。
- 未获明确授权时，不迁移正式数据库、不删除数据库卷、不删除 connector/attestation/checkpoint 数据，也不执行容器内手工在线 DML。
- Discovery 生成代码只能走 gVisor (`runsc`) 沙箱；若不可用，发布/运行应报告阻塞，不能降级为普通 Docker。

## 开发环境

在 `/data/workspace/os-news-tracker-dev` 中操作：

```bash
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml logs -f backend
```

预期服务：`db` healthy、`backend` 使用 `uvicorn --reload`、`frontend` 使用 Vite。访问入口为 `http://localhost:5174`、`http://localhost:8000/docs`。

开发 Compose 将 `ENABLE_SCHEDULER`、`ENABLE_MAIL_SCHEDULER`、`ENABLE_MORNING_CRAWL_SCHEDULER`、`ENABLE_TREND_SCHEDULER` 全部强制为 `0`。不要在开发容器里开启定时任务来替代正式任务。

### 配置或代码变更

源码修改会热更新。修改后端 `.env`、sandbox 运行时映射或后端启动配置后，应重建后端容器：

```bash
docker compose -f docker-compose.dev.yml up -d --force-recreate backend
```

修改前端容器环境或依赖后：

```bash
docker compose -f docker-compose.dev.yml up -d --force-recreate frontend
```

停止环境且保留数据：

```bash
docker compose -f docker-compose.dev.yml down
```

不要使用 `down -v` 作为日常操作。

## 正式发布

### 1. 固化待发布提交

在开发工作区完成变更、必要验证后，提交并推送。正式镜像必须由该确切提交的干净 worktree 构建，绝不从有未提交变更的目录构建。

```bash
git status --short
git add <intended-files>
git commit -m "<release change>"
git push
```

### 2. 创建并选择精确发布 worktree

切换到生产操作 checkout，并同步、校验目标提交：

```bash
cd /data/workspace/os-news-tracker
git fetch --all --prune
release_sha="$(git rev-parse --verify '<commit>^{commit}')"
release_short="${release_sha:0:7}"
release_tree="$(pwd)/.worktrees/release-${release_short}"
if [ -e "$release_tree" ]; then
  test "$(git -C "$release_tree" rev-parse HEAD)" = "$release_sha" \
    || { echo "existing release worktree points at a different commit"; exit 1; }
else
  git worktree add --detach "$release_tree" "$release_sha"
fi
git -C "$release_tree" status --short
export PRODUCTION_WORKTREE="$release_tree"
export PRODUCTION_COMPOSE_PROJECT="os-news-tracker-prod-${release_short}"
```

`docker-compose.production.yml` 的默认 `./.worktrees/production-b4b067c` 是历史 fallback；当前实际版本必须显式提供绝对路径的 `PRODUCTION_WORKTREE`。发布结束前保留该 worktree，以确保镜像来源可追溯。正式容器在构建完成后不依赖它；运行时依赖的是生产操作 checkout 的 connector/Nginx bind mount 和命名卷。

### 3. 发布前检查和构建

确认没有本进程 Discovery 或手动抓取正在运行，再识别当前正式 pair。不要依据容器创建时间或名称猜测目标。

```bash
docker ps --filter label=com.docker.compose.service=backend-prod \
  --filter status=running --format 'table {{.Names}}\t{{.Status}}'
docker ps --filter label=com.docker.compose.service=frontend-prod \
  --filter status=running --format 'table {{.Names}}\t{{.Status}}'
docker compose -p "$PRODUCTION_COMPOSE_PROJECT" -f docker-compose.production.yml build
```

如果 `.env` 有改动，先与开发环境对比所需键、确认敏感值正确，再用本次发布的 `up` 重建正式 backend；不要在聊天或日志中输出值。

### 4. 切换和健康检查

停止当前运行的 `backend-prod` 与 `frontend-prod`，但不要 `rm` 或 `down` 旧 Compose project。旧 pair 必须保留作立即回滚。随后启动新 project：

```bash
docker stop <old-frontend-container> <old-backend-container>
docker compose -p "$PRODUCTION_COMPOSE_PROJECT" -f docker-compose.production.yml up -d
docker compose -p "$PRODUCTION_COMPOSE_PROJECT" -f docker-compose.production.yml ps
curl --fail http://127.0.0.1/
curl --fail http://127.0.0.1/api/docs
curl --fail 'http://127.0.0.1/api/items?limit=1'
```

不要让两个正式 backend 同时运行：它们会同时启动 scheduler。新版本需要通过首页和 API 健康检查后才算切换成功。

### 5. 收尾与回滚

成功后只保留：新运行的前后端 pair，及其紧邻的旧停止 pair。删除更早的**容器**前，重新核对名称和状态；不要删除镜像、网络、数据库卷、connector 数据或批准的 runtime 镜像。

若健康检查失败，停止并保留新 pair，重新启动旧 pair：

```bash
docker stop <new-frontend-container> <new-backend-container>
docker start <old-backend-container> <old-frontend-container>
```

确认旧版本恢复后再诊断；不要通过删除数据卷、重新初始化数据库或拿开发数据覆盖正式数据来“修复”发布。

## 数据库与数据导入

数据库迁移和数据导入是不同操作。应用启动会执行 Alembic；正式数据库迁移、生产数据导入或在线 DML 都需要用户明确授权并遵循批准的数据库变更流程。不要从“开发卷”向“正式卷”复制数据，因为当前拓扑由 Compose project 和网络决定，不能根据名称安全推断。

开发数据备份或恢复应在明确目标数据库、备份文件和恢复范围后单独执行。恢复前先停止写入方并备份当前数据；恢复后再启动 backend 并验证 Alembic 版本。没有明确目标时，先停下并请求确认。
