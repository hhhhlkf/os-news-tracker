# Docker 开发部署说明（迁移提示）

此文件原先记录的端口、数据卷和调度开关已不再准确，不能作为操作依据。

请使用 [部署运行手册](deployment-runbook.md)：其中基于当前容器与 Compose 配置分别说明了开发环境、正式发布、环境变量重载、回滚和数据安全边界。

开发环境的最小启动命令保持为：

```bash
cd /data/workspace/os-news-tracker-dev
docker compose -f docker-compose.dev.yml up -d --build
```

请勿执行裸 `docker compose`，也不要把其他 checkout 的 Compose project、数据库卷或容器当作该开发环境的一部分。
