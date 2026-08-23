"""Discovery 运行取消控制：per-run event + 当前线程上下文。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from threading import Event, Lock


class DiscoveryCancelled(RuntimeError):
    """当前 discovery run 被用户取消。"""


_current_run_id: ContextVar[int | None] = ContextVar("discovery_current_run_id", default=None)
_events: dict[int, Event] = {}
_lock = Lock()


def register_run(run_id: int) -> None:
    """为一次探查运行注册取消事件。

    功能：在全局 _events 字典里为 run_id 新建一个 Event 对象，供后续取消信号与状态查询使用。
    谁会调用：website_workflow.start_website_discovery_run、multi_graph 在 run 启动时调用。
    直接调用：无（仅操作 _events 字典与 _lock）。
    输入与结果：输入 run_id；无返回值。
    副作用：向全局 _events 写入一条 Event（进程级状态变化）。
    """
    with _lock:
        _events[run_id] = Event()


def unregister_run(run_id: int) -> None:
    """移除一次探查运行的取消事件。

    功能：从全局 _events 字典中删除该 run_id 对应的 Event（不存在时忽略）。
    谁会调用：探查流程结束清理时调用（如 website_workflow 运行收尾）。
    直接调用：无（仅操作 _events 字典与 _lock）。
    输入与结果：输入 run_id；无返回值。
    副作用：从全局 _events 删除条目（进程级状态变化）。
    """
    with _lock:
        _events.pop(run_id, None)


def activate_run(run_id: int) -> Token:
    """把当前 run_id 写入上下文变量，便于无参判断取消状态。

    功能：通过 contextvar 记录「当前正在执行的 run」，使后续 ensure_not_cancelled/is_cancelled 不传参也能定位。
    谁会调用：website_workflow、multi_graph 在运行主体开始时调用。
    直接调用：
    - _current_run_id.set(...)：写入上下文变量并返回 Token。
    输入与结果：输入 run_id；返回用于后续重置的 Token。
    副作用：设置当前协程/线程的上下文变量。
    """
    return _current_run_id.set(run_id)


def deactivate_run(token: Token) -> None:
    """重置当前 run_id 上下文。

    功能：用 activate_run 返回的 Token 把上下文变量恢复到进入运行前的状态。
    谁会调用：website_workflow、multi_graph 在运行主体结束时调用。
    直接调用：
    - _current_run_id.reset(...)：重置上下文变量。
    输入与结果：输入 activate_run 返回的 Token；无返回值。
    副作用：重置当前协程/线程的上下文变量。
    """
    _current_run_id.reset(token)


def request_cancel(run_id: int) -> bool:
    """触发某次探查运行的取消信号（用户主动取消）。

    功能：找到该 run_id 对应的 Event 并置位，使运行中的检查点能感知到取消请求。
    谁会调用：discovery_routes.py 的取消接口在收到用户取消请求时调用。
    直接调用：无（仅查表与 Event.set）。
    输入与结果：输入 run_id；返回是否成功置位（run 已注册则返回 True）。
    副作用：将对应 Event 置为 set（进程级状态变化）。
    """
    with _lock:
        event = _events.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def is_cancelled(run_id: int | None = None) -> bool:
    """查询某次（或当前）探查运行是否已被取消。

    功能：未传 run_id 时取上下文中的当前 run，再查其 Event 是否已置位，返回布尔结果。
    谁会调用：ensure_not_cancelled 内部使用，也被 explorer、tools、recipe_writer/audit/url_validation 等循环处轮询。
    直接调用：无（仅查表与 Event.is_set）。
    输入与结果：输入可选 run_id；返回是否被取消的布尔值。
    副作用：无。
    """
    rid = run_id if run_id is not None else _current_run_id.get()
    if rid is None:
        return False
    with _lock:
        event = _events.get(rid)
    return bool(event and event.is_set())


def ensure_not_cancelled() -> None:
    """若当前探查运行已被取消则抛出 DiscoveryCancelled。

    功能：检查当前 run 的取消状态，已取消时抛异常以中断抓取/生成流程。
    谁会调用：website_workflow、explorer、tools、browser_actions、recipe_writer、recipe_audit、url_validation 等阶段与循环入口。
    直接调用：
    - is_cancelled(...)：读取当前 run 是否已取消。
    - DiscoveryCancelled(...)：构造并抛出取消异常。
    输入与结果：无参数；无返回值，否则抛 DiscoveryCancelled 异常。
    副作用：可能抛出异常中断流程（不改变其他状态）。
    """
    if is_cancelled():
        raise DiscoveryCancelled("用户已取消智能探查")
