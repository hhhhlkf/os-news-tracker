"""Plugin audit interfaces plus a deprecated legacy recipe adapter.

Active website and WeChat review uses deterministic connector output and
host-signed sandbox evidence. ``audit_discovery_recipe`` remains below only as
an unused compatibility adapter for the retired graph/DSL implementation.
"""

from __future__ import annotations

from typing import Any

from app.discovery.plugin.contracts import ConnectorOutput
from app.discovery.sandbox.runtime import SandboxExecutionResult, canonical_connector_digest


PLUGIN_AUDIT_SCHEMA_VERSION = 2


def audit_discovery_recipe(
    *,
    source_kind: str,
    input_type: str,
    recipe: dict[str, Any],
    items: list,
    status: str | None = None,
    website_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """DEPRECATED / UNUSED legacy graph/``multi_dsl`` audit interface.

    No production caller remains. Active connector review uses
    ``audit_plugin_trial`` with deterministic evaluation and host-signed
    sandbox evidence. Retain only until legacy cleanup; add no new callers.

    旧功能：website 来源套用图审计结果，其它旧分支走轻量结果审计。
    当前调用者：无。
    直接调用：
    - _finalize_website_audit(...)：规整 website 图审计结果。
    - _audit_lightweight_result(...)：对微信等做轻量结果审计。
    输入与结果：输入 source_kind、input_type、recipe、items、status、website_result；返回含 passed/method_status/reason 等的审计 dict。
    副作用：无。
    """
    discovered_count = len(items)
    if source_kind == "website":
        return _finalize_website_audit(
            website_result or {},
            input_type=input_type,
            discovered_count=discovered_count,
        )
    return _audit_lightweight_result(
        source_kind=source_kind,
        input_type=input_type,
        recipe=recipe,
        discovered_count=discovered_count,
        status=status,
    )


def audit_plugin_trial(
    *,
    evaluation: dict[str, Any],
    outputs: list[ConnectorOutput],
    artifact_evidence: dict[str, Any],
    runtime_attestations: list[dict[str, Any]],
    auxiliary_proofs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit a plugin only from deterministic evaluator and real sandbox evidence.

    This is deliberately separate from the legacy website graph/DSL audit.  It
    never accepts an Agent verdict and never executes a recipe itself.
    """
    checks = list(evaluation.get("checks") or [])
    failures = list(evaluation.get("failures") or [])
    run_counts = [len(output.items) for output in outputs]
    independent_runs = len(outputs) == 2
    artifact_complete = all(
        artifact_evidence.get(key)
        for key in ("connector_key", "version", "checksum", "signature", "runtime_version", "kind")
    )
    evaluator_passed = evaluation.get("passed") is True and not failures
    passed = bool(independent_runs and artifact_complete and evaluator_passed)

    minimum_item_failures = [
        failure for failure in failures
        if str(failure.get("check") or "").endswith(".minimum_items")
    ]
    low_frequency_exception_eligible = bool(
        not passed
        and independent_runs
        and artifact_complete
        and failures
        and len(minimum_item_failures) == len(failures)
        and all(count > 0 for count in run_counts)
        and all(check.get("passed") is True for check in checks if check not in minimum_item_failures)
    )
    status = (
        "passed" if passed
        else "low_frequency_exception_required" if low_frequency_exception_eligible
        else "failed"
    )
    return {
        "schema_version": PLUGIN_AUDIT_SCHEMA_VERSION,
        "audit_kind": "gvisor_plugin_trial",
        "status": status,
        "passed": passed,
        "low_frequency_exception_eligible": low_frequency_exception_eligible,
        "independent_run_count": len(outputs),
        "discovered_counts": run_counts,
        "evaluator": {
            "passed": evaluator_passed,
            "checks": checks,
            "failures": failures,
            "sample_urls": list(evaluation.get("sample_urls") or [])[:10],
        },
        "artifact": dict(artifact_evidence),
        "runtime_attestations": list(runtime_attestations),
        "auxiliary_proofs": list(auxiliary_proofs or []),
        "trial_digests": [
            {
                "trial_index": index,
                "output_sha256": canonical_connector_digest(output),
                "evaluator_input_sha256": canonical_connector_digest(output),
                "quality_input_sha256": canonical_connector_digest(
                    [item.model_dump(mode="json") for item in output.items]
                ),
            }
            for index, output in enumerate(outputs, start=1)
        ],
    }


def sandbox_execution_proof(result: SandboxExecutionResult) -> dict[str, Any]:
    """Persist only the host-redacted invocation/output together with its proof."""
    return {
        "attestation": result.attestation.as_dict(),
        "audit_invocation": result.audit_invocation.model_dump(mode="json"),
        "audit_output": result.audit_output.model_dump(mode="json"),
    }


def _finalize_website_audit(
    result: dict[str, Any],
    *,
    input_type: str,
    discovered_count: int,
) -> dict[str, Any]:
    """DEPRECATED helper used only by the unused ``audit_discovery_recipe``.

    功能：依据 passed/errors/decision 推导 reason，补齐 audit_kind、method_status、required_count 等字段。
    谁会调用：audit_discovery_recipe 的 website 分支调用。
    直接调用：无（仅字典构造与条件判断）。
    输入与结果：输入图审计 result、input_type、discovered_count；返回审计 dict。
    副作用：无。
    """
    passed = bool(result.get("passed"))
    reason = "website_graph_passed" if passed else "website_graph_rejected"
    if result.get("errors"):
        reason = "website_graph_errors"
    elif result.get("test", {}).get("error"):
        reason = "website_graph_runtime_error"
    elif result.get("decision") == "rewrite":
        reason = "website_graph_rewrite"
    elif result.get("decision") == "reexplore":
        reason = "website_graph_reexplore"

    return {
        **result,
        "audit_kind": "website_graph_audit",
        "input_type": input_type,
        "passed": passed,
        "method_status": "active" if passed else "failed",
        "required_count": 1,
        "discovered_count": discovered_count,
        "reason": result.get("reason") or reason,
    }


def _audit_lightweight_result(
    *,
    source_kind: str,
    input_type: str,
    recipe: dict[str, Any],
    discovered_count: int,
    status: str | None,
) -> dict[str, Any]:
    """DEPRECATED helper used only by the unused ``audit_discovery_recipe``.

    功能：先识别鉴权失效/限流等失败状态映射到对应 method_status，否则按 discovered_count 是否达标判定通过与否。
    谁会调用：audit_discovery_recipe 的非 website 分支调用。
    直接调用：
    - _required_count(...)：计算通过所需最少条数。
    - _audit_kind(...)：生成审计类型标识。
    输入与结果：输入各参数；返回审计 dict（passed/method_status/reason 等）。
    副作用：无。
    """
    required_count = _required_count(recipe)
    audit_kind = _audit_kind(source_kind=source_kind, input_type=input_type)
    if status in {"pending_auth", "auth_invalid"}:
        return {
            "audit_kind": audit_kind,
            "input_type": input_type,
            "passed": False,
            "method_status": status,
            "required_count": required_count,
            "discovered_count": discovered_count,
            "reason": status,
        }
    if status in {"rate_limited", "captcha_required"}:
        return {
            "audit_kind": audit_kind,
            "input_type": input_type,
            "passed": False,
            "method_status": "retry_later",
            "required_count": required_count,
            "discovered_count": discovered_count,
            "reason": status,
        }
    if discovered_count >= required_count:
        return {
            "audit_kind": audit_kind,
            "input_type": input_type,
            "passed": True,
            "method_status": "active",
            "required_count": required_count,
            "discovered_count": discovered_count,
            "reason": "enough_items",
        }
    return {
        "audit_kind": audit_kind,
        "input_type": input_type,
        "passed": False,
        "method_status": "failed",
        "required_count": required_count,
        "discovered_count": discovered_count,
        "reason": "insufficient_items",
    }


def _audit_kind(*, source_kind: str, input_type: str) -> str:
    """按来源类型与输入类型生成审计类型标识。

    功能：为微信搜索/历史、website 及其它来源映射出对应的 audit_kind 字符串，便于区分审计口径。
    谁会调用：_audit_lightweight_result 在构造审计结果时调用。
    直接调用：无（仅条件映射）。
    输入与结果：输入 source_kind、input_type；返回 audit_kind 字符串。
    副作用：无。
    """
    if source_kind == "wechat" and input_type == "wechat_search":
        return "wechat_search_audit"
    if source_kind == "wechat" and input_type in {"wechat_history", "wechat_history_url"}:
        return "wechat_history_audit"
    if source_kind == "website":
        return "website_graph_audit"
    return f"{source_kind}_audit"


def _required_count(recipe: dict[str, Any]) -> int:
    """计算通过审计所需的最少条目数。

    功能：从 recipe 的微信搜索/历史动作的 limit 取 25% 作为阈值，并夹在 1~5 之间。
    谁会调用：_audit_lightweight_result 在判定是否达标时调用。
    直接调用：无（仅遍历 actions 与取整）。
    输入与结果：输入 recipe；返回整数阈值。
    副作用：无。
    """
    limit = 20
    for action in recipe.get("actions") or []:
        if action.get("op") in {"wechat_fetch_account_history", "wechat_search_articles"}:
            if action.get("limit") is not None:
                limit = int(action["limit"])
            break
    return min(5, max(1, int(limit * 0.25)))
