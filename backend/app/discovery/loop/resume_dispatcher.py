"""Route durable checkpoints to the correct in-process Loop implementation."""

from __future__ import annotations

from app.discovery.checkpoints import DiscoveryCheckpoint


def dispatch_resumed_loop(run_id: int, checkpoint: DiscoveryCheckpoint) -> None:
    """Keep the internal-forum seam untouched and resume only plugin Discovery paths."""
    if checkpoint.processing_summary.get("discovery_kind") == "wechat":
        from app.discovery.wechat_plugin import get_wechat_discovery_engine

        get_wechat_discovery_engine().resume(run_id, checkpoint)
        return
    from app.discovery.loop.engine import get_website_loop_engine

    get_website_loop_engine().resume(run_id, checkpoint)
