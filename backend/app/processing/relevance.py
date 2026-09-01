from typing import Protocol

from app.discovery.prompts import render_prompt


class _Completer(Protocol):
    def complete(self, prompt: str) -> str: ...


_PROMPT = (
    "你是 AI/Agent 基础设施与 OS/Infra 技术情报的宽入口筛选员。请分别判断 AI 方向和 OS 方向；两者互不依赖，命中任一方向就回答 true。\n"
    "AI 方向：AI、LLM、Agent、Agent Runtime、推理/训练/评测、模型服务、工具调用、沙箱、记忆/RAG、可观测性、数据与部署基础设施，以及相关开源项目的重要发布、能力更新、架构实践或性能数据。AI 内容不需要证明与 Linux 直接相关。\n"
    "OS 方向：Linux、内核、发行版、编译器、运行时、容器、Kubernetes、云原生，以及网络、存储、数据库、调度、虚拟化、构建发布、可观测性等 Infra 技术的重要项目动态。Infra 工程内容不因正文未明确写出 Linux 就直接排除。\n"
    "项目内容可以收录：新项目/开源项目介绍、关键版本、重要功能、架构设计、兼容适配、集成方案、真实性能数据和维护路线，只要包含具体技术事实或工程价值。仓库首页、文档页或教程若能明确说明项目能力、近期变化或可复用工程方案，也应继续进入富集，而不是仅按页面类型排除。\n"
    "这是宽入口预筛选：标题或片段显示可能属于 Agent、AI Infra、OS 或通用 Infra，但信息尚不完整时，回答 true，交给后续富集判断；不要因为不是传统 OS 新闻而回答 false。\n"
    "内核实现、驱动机制、子系统原理和技术教程若包含具体机制、项目能力、性能影响或可复用实践，可以继续处理。\n"
    "漏洞/CVE/安全公告默认从严：仅当正文证明跨社区、跨发行版、上游广泛影响、供应链风险或显著维护决策价值时收录；普通单产品漏洞、例行安全公告和 CVE 罗列排除。\n"
    "仅排除：与上述两个方向都无关的内容，以及纯活动通知、招聘、无技术事实的营销/商业稿、登录/验证码/反爬页面。不要只凭标题中的发布、更新、CVE、AI 或性能词判断。\n"
    "关键词：{keywords}\n"
    "标题：{title}\n"
    "正文片段：{snippet}\n"
    "只回答 true 或 false。"
)


def llm_relevance(title: str, content: str, keywords: str, client: _Completer | None = None) -> bool:
    if client is None:
        from app.llm.client import LlmClient

        client = LlmClient()
    prompt = render_prompt(
        "relevance_filter",
        _PROMPT,
        {
            "{keywords}": keywords,
            "{title}": title or "",
            "{snippet}": content[:800],
        },
    )
    answer = client.complete(prompt).strip().lower()
    return answer.startswith("true")
