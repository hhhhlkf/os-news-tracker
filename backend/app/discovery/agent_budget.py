"""Validated per-run controls for the OpenHands website Discovery loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryAgentBudget(BaseModel):
    """Public four-control contract shared by direct and queued Discovery runs."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    context_memory: int = Field(default=40, alias="contextMemory", ge=20, le=80)
    tool_kb: int = Field(default=48, alias="toolKb", ge=8, le=64)
    depth: int = Field(default=80, ge=20, le=150)
    token_budget: int = Field(
        default=100_000,
        alias="tokenBudget",
        ge=50_000,
        le=200_000,
        multiple_of=10_000,
    )

    def snapshot(self) -> dict[str, int]:
        return self.model_dump(by_alias=True)


def normalize_agent_budget(value: Any = None) -> dict[str, int]:
    """Return a complete immutable-style snapshot using the public field names."""
    if isinstance(value, DiscoveryAgentBudget):
        return value.snapshot()
    return DiscoveryAgentBudget.model_validate(value or {}).snapshot()


DEFAULT_DISCOVERY_AGENT_BUDGET = DiscoveryAgentBudget().snapshot()
