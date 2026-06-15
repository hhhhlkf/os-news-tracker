# 实时日志面板设计文档

**日期：** 2026-06-15
**状态：** 待用户评审
**项目：** `os-news-tracker` 实时日志流

---

## 1. 背景与目标

后端所有进程（手动新闻运行、Digest 生成、Agent 爬取、定时调度）已通过 Python `logging` 模块输出结构化日志到 stdout，但前端只能看到粗粒度的状态计数（发现/处理/保存数）。

本功能将后端日志实时流式推送到前端，以 JetBrains 日志输出风格呈现，让用户直观看到每个操作步骤。

### 覆盖进程

- 手动新闻运行（`app.manual_news_run`、`app.pipeline`）
- Digest 生成（`app.processing.digest_agent`，V2）
- Agent 爬取（`app.agent.*`，V2）
- 定时调度（`app.scheduler`）
- 其余所有 `logging.getLogger(__name__)` 产生的日志

### 明确不做

- 日志持久化到数据库（内存环形缓冲即可，重启清空）
- 日志搜索/过滤持久化历史
- 每条日志绑定「运行 ID」（过于复杂，V1 不做）

---

## 2. 架构概览

```
所有 Python logger
        │
        ▼
  SseLogHandler          ← 自定义 logging.Handler
  (app/log_stream.py)
        │  append(LogEntry)
        ▼
  LogRingBuffer          ← collections.deque(maxlen=2000)
  global deque           ← 线程安全（deque.append 是原子操作）
  每条带自增 seq_no
        │
        ├─── SSE client A  GET /logs/stream  (cursor=seq_no_A)
        ├─── SSE client B  GET /logs/stream  (cursor=seq_no_B)
        └─── SSE client C  GET /logs/stream  (cursor=seq_no_C)
```

**随机-确定性边界**：`SseLogHandler.emit()` 由后台线程调用（logging 是线程安全的），SSE async generator 在事件循环线程运行，两者通过 deque 解耦，无锁竞争。

---

## 3. 后端设计

### 3.1 LogEntry 数据结构

```python
# app/log_stream.py
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class LogEntry:
    seq_no:    int        # 自增序号，SSE 客户端用作游标
    timestamp: str        # ISO8601，精确到毫秒，如 "2026-06-15T14:32:05.123Z"
    level:     str        # "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
    logger:    str        # logger 名，如 "app.pipeline" 或 "app.agent.plan_agent"
    message:   str        # 日志消息文本
```

### 3.2 LogRingBuffer + SseLogHandler

```python
# app/log_stream.py
import collections
import logging
import threading

_SEQ = 0
_SEQ_LOCK = threading.Lock()

# 保留最新 2000 条，超出自动丢弃最旧条目
_buffer: collections.deque[LogEntry] = collections.deque(maxlen=2000)


def _next_seq() -> int:
    global _SEQ
    with _SEQ_LOCK:
        _SEQ += 1
        return _SEQ


class SseLogHandler(logging.Handler):
    """把所有日志追加到全局 ring buffer，供 SSE 客户端消费。"""

    # 过滤噪声日志（uvicorn 访问日志、SQLAlchemy echo 等）
    SKIP_LOGGERS = {"uvicorn.access", "sqlalchemy.engine", "httpx"}

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in self.SKIP_LOGGERS:
            return
        entry = LogEntry(
            seq_no=_next_seq(),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") +
                       f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
            level=record.levelname,
            logger=record.name,
            message=self.format(record),
        )
        _buffer.append(entry)


def get_entries_since(seq_no: int) -> list[LogEntry]:
    """返回 seq_no 之后的所有条目（线程安全，deque 快照）。"""
    return [e for e in list(_buffer) if e.seq_no > seq_no]


def install_handler(level: int = logging.DEBUG) -> None:
    """在应用启动时调用一次，安装到 root logger。"""
    handler = SseLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))  # 消息本身不加前缀，字段已结构化
    logging.getLogger().addHandler(handler)
```

### 3.3 在 app 启动时安装

```python
# app/entry.py，在 create_app() 中调用
from app.log_stream import install_handler

def create_app():
    install_handler(level=logging.INFO)  # 生产环境 INFO 及以上
    ...
```

### 3.4 SSE 端点

```python
# app/api/routes.py（新增）
import asyncio
import json
from fastapi.responses import StreamingResponse
from app.log_stream import get_entries_since

@router.get("/logs/stream")
async def stream_logs(request: Request, since: int = 0):
    # 支持 EventSource 断线重连时的 Last-Event-ID header
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id is not None:
        try:
            since = int(last_event_id)
        except ValueError:
            pass
    """
    SSE 端点，持续推送 seq_no > since 的新日志条目。
    客户端断开后 generator 自动终止。
    """
    async def _event_generator():
        cursor = since
        # 先把缓冲区中已有的条目推一次（客户端刷新后补历史）
        for entry in get_entries_since(cursor):
            yield _format_sse(entry)
            cursor = entry.seq_no

        # 然后每 100ms 检查新条目
        while True:
            await asyncio.sleep(0.1)
            new_entries = get_entries_since(cursor)
            for entry in new_entries:
                yield _format_sse(entry)
                cursor = entry.seq_no

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


def _format_sse(entry) -> str:
    data = json.dumps({
        "seq_no":    entry.seq_no,
        "timestamp": entry.timestamp,
        "level":     entry.level,
        "logger":    entry.logger,
        "message":   entry.message,
    }, ensure_ascii=False)
    # 使用 SSE 标准 `id:` 字段，浏览器断线重连时会自动携带 Last-Event-ID header，
    # 服务端用 request.headers.get("Last-Event-ID", "0") 恢复游标，无需客户端手动管理。
    return f"id: {entry.seq_no}\ndata: {data}\n\n"
```

**安全注意**：V2 引入用户鉴权后，`/logs/stream` 应要求有效 JWT（仅限 admin 或已登录用户可见）。V1 先不加鉴权。

### 3.5 CORS 配置补充

SSE 端点需要确保 `Access-Control-Allow-Origin` 头正确，现有 CORS 中间件已处理，无需额外配置。

---

## 4. 前端设计

### 4.1 LogEntry 类型

```typescript
// frontend/src/types.ts（新增）
export interface LogEntry {
  seq_no:    number;
  timestamp: string;   // ISO8601
  level:     "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  logger:    string;
  message:   string;
}
```

### 4.2 useLogStream Hook

```typescript
// frontend/src/hooks/useLogStream.ts
import { useEffect, useRef, useState } from "react";
import type { LogEntry } from "../types";

const MAX_ENTRIES = 500;  // 前端最多保留 500 条，防止 DOM 过多

export function useLogStream(enabled: boolean) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!enabled) return;

    // 首次连接传 since=0；断线重连时浏览器会自动携带 Last-Event-ID，
    // 服务端据此恢复游标，客户端无需手动维护 cursor。
    const es = new EventSource("/api/logs/stream");
    esRef.current = es;

    es.onmessage = (ev) => {
      const entry: LogEntry = JSON.parse(ev.data as string);
      setEntries((prev) => {
        const next = [...prev, entry];
        // 超出上限时丢弃最旧条目
        return next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next;
      });
    };

    es.onerror = () => {
      // EventSource 会自动重连，无需手动处理
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [enabled]);

  const clear = () => setEntries([]);

  return { entries, clear };
}
```

**自动滚动**：`useLogStream` 不负责滚动，由 `LogPanel` 组件处理。

### 4.3 LogPanel 组件

```typescript
// frontend/src/components/LogPanel.tsx
```

**布局**：

```
┌── LogPanel ─────────────────────────────────────────────────┐
│  [● LIVE]  日志输出                            [清空] [×关闭]│
├─────────────────────────────────────────────────────────────┤
│  14:32:05.123  INFO    app.pipeline        fetch started    │
│  14:32:05.847  INFO    app.pipeline        fetched 12 items │
│  14:32:06.102  WARNING app.pipeline        item has no date │
│  14:32:07.301  INFO    app.agent.plan      plan: 8 urls     │
│  14:32:08.004  ERROR   app.agent.quality   llm call failed  │
│  ▌                                                ← 光标闪烁 │
└─────────────────────────────────────────────────────────────┘
```

**日志行样式（按 level 着色）：**

| Level | 行前景色 | Logger 颜色 | 风格参考 |
|-------|---------|------------|---------|
| DEBUG | `#7d8b9a`（muted）| `#94a3b8` | 灰色暗显 |
| INFO | `#d4dae3`（近白）| `#60a5fa`（蓝）| 正常输出 |
| WARNING | `#fbbf24`（amber）| `#fbbf24` | 黄色警告 |
| ERROR | `#f87171`（红）| `#f87171` | 红色错误 |
| CRITICAL | `#ffffff`（白）| `#dc2626`（红粗）| 反色强调 |

**背景**：`#0d1117`（与设计系统的 `--ink` 一致，深色终端感）

**字体**：整个面板使用 `JetBrains Mono 13px`（与设计系统 mono 字体一致）

**字段排版**（固定宽度列，单行不换行，超长 message 省略号）：
```
HH:mm:ss.SSS  LEVEL    logger.name          message text here...
└─12char──┘  └─8ch─┘  └────20char───────┘  └────── flex ──────
```

### 4.4 自动滚动行为

- 默认自动滚到底部（最新日志）
- 用户向上滚动时暂停自动滚动，显示「⬇ 有新日志」浮动按钮
- 点击按钮或滚回底部时恢复自动滚动

```typescript
// 实现方式：ref 监测 scrollTop + scrollHeight
const isAtBottom = containerRef.scrollHeight - containerRef.scrollTop < containerRef.clientHeight + 50;
```

### 4.5 集成到现有页面

**LogPanel 的显示位置**：浮动在页面右下角，可展开/收起。

```
┌──────────────────────────────────────────────────────────┐
│  （页面主体内容）                                         │
│                                                          │
│                                    ┌──────────────────┐  │
│                                    │ LogPanel（展开态）│  │
│                                    │ 高度 300px       │  │
│                                    │ 宽度 560px       │  │
│                                    └──────────────────┘  │
│                                    [● 日志] ← 收起时显示 │
└──────────────────────────────────────────────────────────┘
```

- 右下角固定定位（`position: fixed; right: 20px; bottom: 20px`）
- 收起态：一个 24px 高的黑色细条 + `● LIVE` 绿点 + 最近一条日志摘要（单行 truncate）
- 展开态：300px 高，560px 宽（移动端 100vw - 32px）
- `● LIVE` 绿点：当有活跃进程运行时呼吸动效（`animation: pulse 2s infinite`）；空闲时静止

### 4.6 与 NewsRunControl 的联动

当手动新闻运行处于 `collecting / processing` 状态时，LogPanel 自动展开（不需要用户手动点击）。运行结束后不自动收起，用户手动收起。

---

## 5. 文件变更汇总

### 后端（新增）

```
backend/app/log_stream.py       ← LogEntry, LogRingBuffer, SseLogHandler, install_handler
backend/app/api/routes.py       ← 新增 GET /logs/stream 端点
backend/app/entry.py            ← 在 create_app() 中调用 install_handler()
```

### 前端（新增）

```
frontend/src/hooks/useLogStream.ts   ← EventSource hook
frontend/src/components/LogPanel.tsx ← 日志面板组件
frontend/src/types.ts                ← 新增 LogEntry 接口
frontend/src/pages/HomePage.tsx      ← 集成 LogPanel（右下角固定浮层）
```

---

## 6. 测试策略

| 类型 | 覆盖范围 |
|------|---------|
| 单元测试 | `SseLogHandler.emit()` 正确追加到 deque；`get_entries_since()` 游标过滤；deque 超出 maxlen 自动丢弃 |
| 集成测试 | `GET /logs/stream` 返回 `text/event-stream` Content-Type；连接后推送初始缓冲区内容；新日志到来时推送新条目 |
| 前端测试 | `useLogStream` mock EventSource；LogPanel 自动滚动逻辑；MAX_ENTRIES 截断 |

---

## 7. 待确认项

- [ ] `SseLogHandler` 安装的 log level：生产建议 `INFO`，开发时改为 `DEBUG`（通过 env var `LOG_STREAM_LEVEL` 控制）
- [ ] 日志面板中是否需要按 level 过滤按钮（如只看 ERROR）？V1 不做，先全量展示
- [ ] `/logs/stream` 在 V2 用户系统上线后是否需要鉴权？建议：仅限已登录用户，或仅 admin 可见
- [ ] `SKIP_LOGGERS` 名单是否完整（现有：`uvicorn.access`, `sqlalchemy.engine`, `httpx`）
