# 02 — Embedding 接入预检与版本状态

**What to build:** 用户可从趋势工作台准备并确认本地 `Qwen/Qwen3-Embedding-0.6B` 是否可用，看到模型缓存、运行状态与向量版本；业务接口保持对未来网关或其他本地模型的可替换性。

**Blocked by:** 01 — 趋势总结工作台与身份模板.

**Status:** ready-for-agent

**Module boundary:** Embedding Provider、模型缓存状态和按需 Worker 均归 `backend/app/trends/`；不得加载到既有 Web 进程或改动 Agent Crawl。

- [ ] 模型未安装时，Worker 可将官方 Qwen 模型下载到持久化缓存卷，并在工作台显示未安装、下载中、就绪或失败状态。
- [ ] Worker 按需以 CPU 模式加载模型、批量生成 1024 维归一化向量并在任务结束后释放内存；不得加载在后端 Web 进程中。
- [ ] 工作台显示当前模型、模型版本和向量版本，失败时给出可操作的错误说明；业务侧仍通过统一接口调用，避免供应商特定依赖。
