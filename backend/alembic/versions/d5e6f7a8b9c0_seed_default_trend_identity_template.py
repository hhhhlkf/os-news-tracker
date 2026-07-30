"""seed default trend identity template

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-29 12:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_TEMPLATE_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_TEMPLATE_NAME = "新闻趋势分析师"
DEFAULT_IDENTITY_TEXT = (
    "我是新闻趋势分析师，聚焦操作系统与人工智能领域的可验证技术变化。重点识别 Linux、云原生、"
    "系统软件、基础设施、芯片与开发工具，以及大模型、Agent、推理、训练、开源生态和 AI 落地的持续演进。"
    "基于跨来源、跨主体和时间窗口证据判断新兴趋势、热点、主线与降温信号；优先关注技术成熟度、生态影响、"
    "安全与成本、兼容性和实际部署。忽略单纯营销、缺少证据的预测及孤立新闻。"
)


def upgrade() -> None:
    trend_identity_templates = sa.table(
        "trend_identity_templates",
        sa.column("template_id", sa.String),
        sa.column("name", sa.String),
        sa.column("identity_text", sa.Text),
    )
    op.bulk_insert(
        trend_identity_templates,
        [
            {
                "template_id": DEFAULT_TEMPLATE_ID,
                "name": DEFAULT_TEMPLATE_NAME,
                "identity_text": DEFAULT_IDENTITY_TEXT,
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM trend_identity_templates WHERE template_id = :template_id").bindparams(
            template_id=DEFAULT_TEMPLATE_ID
        )
    )
