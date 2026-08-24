# Discovery replacement boundary

This inventory documents the code-retirement boundary after website and
WeChat discovery moved to versioned Python connectors. `DEPRECATED` comments
are documentation only: they do not change runtime behavior or remove HTTP
compatibility.

## Active interfaces

- `/discovery/run`, `/discovery/multi-run`, run query/cancel/resume, event SSE
- method list/detail/review/delete/enable/formal-fetch interfaces
- `multi_graph.py` and `multi_routes.py` as the stable website/WeChat/internal
  forum routing facade
- `loop/`, `wechat_plugin.py`, `plugin/`, `sandbox/`, review, recovery,
  migration, events, checkpoints, and the formal runner
- `audit_plugin_trial` and the deterministic plugin quality/review interfaces

## Deprecated but retained for migration or rollback

- `dsl.py`, `interpreter.py`, `multi_dsl.py`, `multi_interpreter.py`
- `tools.py`, `browser_actions.py`, `article_tools.py`
- the explicit legacy branch behind `execution.run_method(...,
  allow_legacy_compatibility=True)`
- legacy recipe mutations in `recipe_prepare.py`

These modules must not receive new Discovery behavior. They can be removed
only after every legacy method has passed the migration cleanup-readiness gate.

## Deprecated dormant WeChat authentication path

- `wechat_tools.py`
- `wechat_auth.py`
- `/wechat-auth/*` in `api/wechat_auth_routes.py`
- `scripts/diagnose_wechat_history.py`
- `WechatAuthPanel.tsx`

The endpoints and UI remain available only for compatibility. Active WeChat
discovery/formal execution uses the anonymous reviewed shared connector and
does not consume MP cookies or tokens.

## Deprecated and currently unused

- `graph.py`: retired import tombstone used only by opt-in legacy tests
- `multi_trace.py`: no production imports; replaced by persisted events and
  checkpoints
- `audit.audit_discovery_recipe` and its graph/lightweight helpers: no
  production callers; replaced by plugin trial audit

Do not delete these files as part of annotation-only work. Physical deletion
requires a separate cleanup task and verification of the internal-forum seam,
stored recipes, rollback eligibility, and production data state.
