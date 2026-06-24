"""XHR 候选 API 的 Agent 选择器：用 LLM 从候选列表中选择最适合抓取的 API。"""

from __future__ import annotations

import json
from typing import Any

from app.llm.client import LlmClient

_AGENT_PROMPT = """你是 OS News Tracker 的来源抓取配置选择 agent。

任务：
根据网页 URL 和 XHR/Fetch 探测得到的 JSON API 候选，选择最适合新闻、博客、公告抓取的 API，并生成 api_config.probe。

选择标准：
1. 优先选择直接返回内容列表的 API。
2. 优先选择字段完整的 API：title、url/path、date、summary/content。
3. 优先选择请求简单的 API：无需登录、无需动态 token、无需复杂 cookie。
4. 优先选择支持分页或稳定返回最新内容的 API。
5. 排除统计、标签、搜索建议、埋点、用户行为、静态配置类 API。
6. 不允许选择候选列表之外的 API。
7. 如果没有合适候选，selected_index 返回 null，并说明原因。

输出必须是 JSON，不要输出额外文本。

网页 URL：{page_url}

候选 API 列表：
{candidates_json}

只输出以下 JSON 结构：
{{
  "selected_index": number | null,
  "reason": string,
  "api_config": {{
    "mode": "json_list",
    "method": "GET" | "POST",
    "url": string,
    "headers": object,
    "query": object,
    "json_body": object | null,
    "items_path": string,
    "fields": {{
      "title": string,
      "url": string,
      "url_template": string | null,
      "published_at": string | null,
      "content": string | null
    }}
  }},
  "field_mapping": object,
  "pagination": {{
    "type": "page" | "cursor" | "none",
    "page_param": string | null,
    "next_path": string | null
  }},
  "rejected_candidates": [{{"index": number, "reason": string}}],
  "confidence": "high" | "medium" | "low"
}}
"""


def select_best_xhr_candidate(
    page_url: str,
    candidates: list[dict[str, Any]],
    *,
    llm: LlmClient | None = None,
) -> dict[str, Any]:
    """让 LLM 从候选列表中选择最适合的 API，返回结构化 JSON。"""
    if not candidates:
        return {
            "selected_index": None,
            "reason": "未探测到任何 JSON API 候选",
            "api_config": None,
            "field_mapping": {},
            "pagination": {"type": "none", "page_param": None, "next_path": None},
            "rejected_candidates": [],
            "confidence": "low",
        }

    llm = llm or LlmClient()
    candidates_json = json.dumps(candidates, ensure_ascii=False, indent=2)
    prompt = _AGENT_PROMPT.format(page_url=page_url, candidates_json=candidates_json)
    raw = llm.complete(prompt, temperature=0.1).strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        start = 1
        end = len(lines) - 1
        for i in range(1, len(lines)):
            if lines[i].strip().startswith("```"):
                end = i
                break
        raw = "\n".join(lines[start:end])

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "selected_index": None,
            "reason": "Agent 返回格式异常，无法解析",
            "api_config": None,
            "field_mapping": {},
            "pagination": {"type": "none", "page_param": None, "next_path": None},
            "rejected_candidates": [],
            "confidence": "low",
            "raw_response": raw[:500],
        }

    # Validate selected_index is within range
    idx = result.get("selected_index")
    if idx is not None and (not isinstance(idx, int) or idx < 0 or idx >= len(candidates)):
        result["selected_index"] = None
        result["reason"] = f"Agent 选择了无效索引 {idx}，已忽略"

    return result
