from html import escape


def build_mail_preview_context(*, filters: dict, items: list[dict], subject: str) -> dict:
    return {
        "subject": subject,
        "filters": filters,
        "total_items": len(items),
        "items": items,
    }


def render_mail_html(context: dict) -> str:
    subject = escape(str(context.get("subject") or "新闻简报"))
    filters = context.get("filters") or {}
    items = context.get("items") or []
    summary_parts = []
    if filters.get("main_category"):
        summary_parts.append(str(filters["main_category"]))
    if filters.get("sub_tag"):
        summary_parts.append(str(filters["sub_tag"]))
    if filters.get("q"):
        summary_parts.append(f"关键词：{filters['q']}")
    summary_text = " · ".join(summary_parts) if summary_parts else "当前筛选结果"

    cards = []
    for item in items:
        title = escape(str(item.get("title") or "未命名新闻"))
        reason = escape(str(item.get("reason") or ""))
        summary = escape(str(item.get("summary") or ""))
        published_at = escape(str(item.get("published_at") or ""))
        source_url = escape(str(item.get("source_url") or ""))
        hotspots = " / ".join(escape(str(h)) for h in item.get("hotspots") or [])
        key_points = "".join(
            f"<li>{escape(str(point))}</li>"
            for point in (item.get("key_points") or [])
        )
        cards.append(
            f"""
            <section style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px 20px;margin-bottom:14px;">
              <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;">
                <h2 style="margin:0;font-size:18px;line-height:1.45;color:#101828;">{title}</h2>
                <span style="font-size:12px;color:#667085;white-space:nowrap;">{published_at}</span>
              </div>
              <div style="margin-top:12px;color:#344054;font-size:14px;line-height:1.75;">
                <p style="margin:0 0 8px;"><strong>推荐理由：</strong>{reason or "暂无"}</p>
                <p style="margin:0 0 8px;"><strong>摘要：</strong>{summary or "暂无"}</p>
                <p style="margin:0 0 8px;"><strong>技术热点：</strong>{hotspots or "暂无"}</p>
                <div style="margin:0 0 8px;"><strong>技术要点：</strong><ul style="margin:6px 0 0 20px;padding:0;">{key_points or '<li>暂无</li>'}</ul></div>
                <p style="margin:0;"><strong>来源链接：</strong><a href="{source_url}" style="color:#175cd3;text-decoration:none;">{source_url}</a></p>
              </div>
            </section>
            """
        )

    return f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{subject}</title>
    </head>
    <body style="margin:0;padding:24px;background:#f5f7fb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#101828;">
      <main style="max-width:960px;margin:0 auto;">
        <header style="background:linear-gradient(135deg,#101828 0%,#1f2937 100%);color:#f8fafc;border-radius:18px;padding:24px 28px;margin-bottom:18px;">
          <div style="font-size:12px;color:#98a2b3;margin-bottom:8px;">OS News Tracker</div>
          <h1 style="margin:0 0 8px;font-size:28px;line-height:1.2;">{subject}</h1>
          <p style="margin:0;color:#d0d5dd;font-size:14px;line-height:1.7;">{escape(summary_text)}<br/>共 {len(items)} 条，按当前筛选生成。</p>
        </header>
        {''.join(cards) if cards else '<section style="background:#fff;border:1px dashed #d0d5dd;border-radius:14px;padding:24px;color:#667085;">当前筛选下暂无可发送新闻。</section>'}
      </main>
    </body>
    </html>
    """
