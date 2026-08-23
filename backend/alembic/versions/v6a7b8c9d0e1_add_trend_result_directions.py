"""add template-bound directions to trend runs and results

Revision ID: v6a7b8c9d0e1
Revises: u5f6a7b8c9d0
Create Date: 2026-08-19 11:20:00.000000
"""

from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "u5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_DIRECTIONS = " 分类方向：{AI方向}、{OS方向}；每条趋势必须归入其中一个方向。"
_DIRECTION_TOKEN = re.compile(r"\{([^{}]*)\}")
_AI_TERMS = ("ai", "人工智能", "大模型", "模型", "agent", "llm", "推理", "训练", "机器学习", "生成式")
_OS_TERMS = ("操作系统", "系统软件", "linux", "内核", "kernel", "云原生", "容器", "kubernetes", "发行版", "虚拟化", "驱动", "文件系统")


def _valid_label(value: str) -> str | None:
    label = value.strip()
    if not label or "\n" in label or "\r" in label or len(label) > 50:
        return None
    return label


def _sanitize_and_extract(identity_text: str) -> tuple[str, list[str]]:
    """Make old free-form braces harmless and retain only valid declarations."""
    pieces: list[str] = []
    labels: list[str] = []
    seen: set[str] = set()
    cursor = 0
    for match in _DIRECTION_TOKEN.finditer(identity_text):
        pieces.append(identity_text[cursor:match.start()].replace("{", "（").replace("}", "）"))
        label = _valid_label(match.group(1))
        if label is None:
            pieces.append(match.group(0).replace("{", "（").replace("}", "）"))
        else:
            pieces.append("{" + label + "}")
            if label not in seen and len(labels) < 12:
                seen.add(label)
                labels.append(label)
        cursor = match.end()
    pieces.append(identity_text[cursor:].replace("{", "（").replace("}", "）"))
    cleaned = "".join(pieces).strip()
    if not labels:
        cleaned = cleaned.rstrip() + DEFAULT_DIRECTIONS
        labels = ["AI方向", "OS方向"]
    return cleaned, labels


def _legacy_direction(labels: list[str], text: str) -> str:
    """Deterministically classify legacy output without making migration-time LLM calls."""
    if len(labels) == 1:
        return labels[0]
    normalized = text.lower()
    ai_labels = [label for label in labels if any(term in label.lower() for term in _AI_TERMS)]
    os_labels = [label for label in labels if any(term in label.lower() for term in _OS_TERMS)]
    if ai_labels and os_labels:
        ai_score = sum(normalized.count(term) for term in _AI_TERMS)
        os_score = sum(normalized.count(term) for term in _OS_TERMS)
        return ai_labels[0] if ai_score > os_score else os_labels[0]
    return labels[0]


def upgrade() -> None:
    op.add_column(
        "trend_runs",
        sa.Column("analysis_identity_snapshot", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "trend_runs",
        sa.Column("direction_labels", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("trend_results", sa.Column("direction", sa.String(length=50), nullable=True))
    op.create_index("ix_trend_results_direction", "trend_results", ["direction"])

    bind = op.get_bind()
    metadata = sa.MetaData()
    templates = sa.Table("trend_identity_templates", metadata, autoload_with=bind)
    runs = sa.Table("trend_runs", metadata, autoload_with=bind)
    results = sa.Table("trend_results", metadata, autoload_with=bind)

    # Every existing template gains a valid declaration.  Malformed old braces
    # become full-width prose punctuation, so users can immediately edit or run
    # the template through the same strict parser as newly created templates.
    template_data: dict[str, tuple[str, list[str]]] = {}
    for row in bind.execute(sa.select(templates.c.template_id, templates.c.identity_text)).mappings():
        identity_text, labels = _sanitize_and_extract(row["identity_text"])
        template_data[row["template_id"]] = (identity_text, labels)
        bind.execute(
            sa.update(templates)
            .where(templates.c.template_id == row["template_id"])
            .values(identity_text=identity_text)
        )

    direction_by_run: dict[str, list[str]] = {}
    for row in bind.execute(sa.select(runs.c.run_id, runs.c.template_id)).mappings():
        identity_text, labels = template_data[row["template_id"]]
        direction_by_run[row["run_id"]] = labels
        bind.execute(
            sa.update(runs)
            .where(runs.c.run_id == row["run_id"])
            .values(analysis_identity_snapshot=identity_text, direction_labels=labels)
        )

    # Historical result text is classified deterministically, never by a
    # migration-time LLM call.  The label is always one declared by that run's
    # template, making legacy results visible in AI/OS filters immediately.
    for row in bind.execute(
        sa.select(
            results.c.result_id,
            results.c.run_id,
            results.c.topic,
            results.c.trend_summary,
            results.c.agent_review,
        ).where(results.c.direction.is_(None))
    ).mappings():
        labels = direction_by_run[row["run_id"]]
        text = " ".join(value or "" for value in (row["topic"], row["trend_summary"], row["agent_review"]))
        bind.execute(
            sa.update(results)
            .where(results.c.result_id == row["result_id"])
            .values(direction=_legacy_direction(labels, text))
        )


def downgrade() -> None:
    op.drop_index("ix_trend_results_direction", table_name="trend_results")
    op.drop_column("trend_results", "direction")
    op.drop_column("trend_runs", "direction_labels")
    op.drop_column("trend_runs", "analysis_identity_snapshot")
