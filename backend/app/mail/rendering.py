from html import escape


def build_mail_preview_context(*, filters: dict, items: list[dict], subject: str) -> dict:
    return {
        "subject": subject,
        "filters": filters,
        "total_items": len(items),
        "items": items,
    }


def _format_date_ymd(value: str | None) -> str:
    if not value:
        return "-"
    return str(value)[:10]


def _parse_tech_highlight(text: str) -> tuple[str | None, str]:
    raw = str(text or "")
    if raw.startswith("[") and "]" in raw:
        keyword, rest = raw[1:].split("]", 1)
        return keyword.strip() or None, rest.strip()
    return None, raw


def _build_header_summary(filters: dict) -> str:
    segments: list[str] = []
    if filters.get("main_category"):
        segments.append(str(filters["main_category"]))
    if filters.get("importance"):
        segments.append(str(filters["importance"]))
    if filters.get("sub_tag"):
        segments.append(str(filters["sub_tag"]))
    if filters.get("q"):
        segments.append(str(filters["q"]))

    published_label = "全部时间"
    if filters.get("published_after_mode") == "relative" and filters.get("published_after_value"):
        value = str(filters.get("published_after_value") or "")
        labels = {"24h": "最近 24h", "7d": "最近 7d", "30d": "最近 30d"}
        published_label = labels.get(value, f"最近 {value}")
    elif filters.get("published_after") or filters.get("published_before"):
        after = str(filters.get("published_after") or "").strip()
        before = str(filters.get("published_before") or "").strip()
        if after or before:
            published_label = f"{'从 ' + after if after else ''}{' ' if after and before else ''}{'到 ' + before if before else ''}".strip()
    segments.append(published_label)
    return " · ".join(segment for segment in segments if segment) or "依据当前筛选快照生成"


def _render_hotspot_tags(tags: list[str]) -> str:
    if not tags:
        return '<div style="font-size:13px;color:#98a2b3;">暂无</div>'
    return (
        '<div style="display:flex;flex-wrap:wrap;gap:6px;">'
        + "".join(
            f'<span style="background:#f2f4f7;border-radius:6px;padding:2px 8px;font-size:12px;color:#344054;">{escape(str(tag))}</span>'
            for tag in tags
        )
        + "</div>"
    )


def _render_importance_badge(value: str | None) -> str:
    if not value:
        return '<span style="font-size:12px;color:#98a2b3;">重要性未标注</span>'
    styles = {
        "高": ("#fde2e1", "#b42318"),
        "中": ("#fef3c7", "#92400e"),
        "低": ("#eceef1", "#475467"),
    }
    bg, fg = styles.get(str(value), styles["低"])
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        'padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap;">'
        f'{escape(str(value))}</span>'
    )


def _render_source_cta(url: str) -> str:
    safe_url = escape(url)
    domain = url
    try:
        from urllib.parse import urlparse

        domain = urlparse(url).hostname or url
        if domain.startswith("www."):
            domain = domain[4:]
    except Exception:
        domain = url
    if len(domain) > 10:
        domain = f"{domain[:9]}…"
    safe_domain = escape(domain)
    return f"""
    <a
      href="{safe_url}"
      style="display:inline-flex;align-items:center;gap:6px;height:28px;box-sizing:border-box;padding:6px 9px;border:1px solid #d0d5dd;border-radius:8px;background:#fff;color:#344054;font-size:12px;text-decoration:none;line-height:1;max-width:168px;width:fit-content;white-space:nowrap;"
    >
      <span>阅读原文</span>
      <span style="color:#98a2b3;max-width:80px;overflow:hidden;text-overflow:ellipsis;">{safe_domain}</span>
    </a>
    """


def _quality_grade_for_score(score: int | None) -> str | None:
    if score is None:
        return None
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _render_source_quality(item: dict) -> str:
    source_name = str(item.get("source_name") or "").strip()
    score = item.get("source_quality_score")
    try:
        score_value = int(score) if score is not None else None
    except (TypeError, ValueError):
        score_value = None
    status = str(item.get("source_quality_status") or "")
    grade = str(item.get("source_quality_grade") or _quality_grade_for_score(score_value) or "")

    source_html = (
        f'<span style="font-size:12px;color:#475467;max-width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{escape(source_name)}</span>'
        if source_name
        else '<span style="font-size:12px;color:#98a2b3;">来源未标注</span>'
    )
    if score_value is None:
        badge_style = (
            "min-width:58px;text-align:center;border-radius:8px;padding:3px 7px;font-size:12px;"
            "font-weight:800;line-height:1.1;white-space:nowrap;border:1px solid #d0d5dd;"
            "color:#475467;background:#f9fafb;"
        )
        label = "未审计"
    else:
        if status == "failed" or score_value < 50:
            colors = ("#fecdca", "#b42318", "#fef3f2")
        elif status == "weak" or score_value < 70:
            colors = ("#fedf89", "#b54708", "#fffaeb")
        else:
            colors = ("#abefc6", "#027a48", "#ecfdf3")
        badge_style = (
            "min-width:58px;text-align:center;border-radius:8px;padding:3px 7px;font-size:12px;"
            f"font-weight:800;line-height:1.1;white-space:nowrap;border:1px solid {colors[0]};"
            f"color:{colors[1]};background:{colors[2]};"
        )
        label = f"{grade} {score_value}".strip()
    return (
        '<div style="display:inline-flex;align-items:center;gap:8px;height:28px;box-sizing:border-box;min-width:0;max-width:320px;'
        'padding:6px 9px;border:1px solid #d0d5dd;border-radius:8px;background:#fff;line-height:1;">'
        f"{source_html}"
        f'<span style="{badge_style}">{escape(label)}</span>'
        "</div>"
    )


def _render_tech_highlights(points: list[str]) -> str:
    visible_points = [str(point) for point in points if str(point) and not str(point).startswith("__type:")]
    if not visible_points:
        return '<div style="font-size:13px;color:#98a2b3;">暂无</div>'

    cards: list[str] = []
    for point in visible_points:
        keyword, detail = _parse_tech_highlight(point)
        if keyword:
            body = f'<strong style="color:#175cd3;">[{escape(keyword)}]</strong> {escape(detail)}'
        else:
            body = escape(detail)
        cards.append(
            f'<div style="border:1px solid #eaecf0;border-radius:8px;padding:7px 10px;margin-bottom:6px;background:#fafafa;font-size:13px;color:#475467;line-height:1.65;">{body}</div>'
        )
    return "".join(cards)


def render_mail_html(context: dict) -> str:
    subject = escape(str(context.get("subject") or "新闻简报"))
    filters = context.get("filters") or {}
    items = context.get("items") or []
    summary_text = _build_header_summary(filters)

    cards = []
    for item in items:
        title = escape(str(item.get("title") or "未命名新闻"))
        summary = escape(str(item.get("summary") or ""))
        importance_html = _render_importance_badge(item.get("importance"))
        published_at = _format_date_ymd(item.get("published_at"))
        source_url = str(item.get("source_url") or "")
        hotspots_html = _render_hotspot_tags([str(h) for h in (item.get("hotspots") or [])])
        key_points_html = _render_tech_highlights([str(point) for point in (item.get("key_points") or [])])
        source_cta_html = _render_source_cta(source_url) if source_url else '<div style="font-size:13px;color:#98a2b3;">暂无</div>'
        source_quality_html = _render_source_quality(item)
        cards.append(
            f"""
            <section style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px 20px;margin-bottom:14px;">
              <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;">
                <h2 style="margin:0;font-size:18px;line-height:1.45;color:#101828;">{title}</h2>
                <div style="display:flex;gap:8px;align-items:center;white-space:nowrap;">
                  {importance_html}
                  <span style="font-size:12px;color:#667085;">{escape(published_at)}</span>
                </div>
              </div>
              <div style="margin-top:12px;display:grid;gap:10px;color:#344054;font-size:14px;line-height:1.75;">
                <p style="margin:0 0 8px;"><strong>摘要：</strong>{summary or "暂无"}</p>
                <div>
                  <strong style="color:#101828;display:block;margin-bottom:6px;">技术要点</strong>
                  {key_points_html}
                </div>
                <div>
                  <strong style="color:#101828;display:block;margin-bottom:6px;">技术热点</strong>
                  {hotspots_html}
                </div>
                <div>
                  <strong style="color:#101828;display:block;margin-bottom:6px;">来源链接</strong>
                  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    {source_cta_html}
                    {source_quality_html}
                  </div>
                </div>
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
          <p style="margin:0;color:#d0d5dd;font-size:14px;line-height:1.7;">筛选条件：{escape(summary_text)}<br/>共 {len(items)} 条，按当前筛选生成。</p>
        </header>
        {''.join(cards) if cards else '<section style="background:#fff;border:1px dashed #d0d5dd;border-radius:14px;padding:24px;color:#667085;">当前筛选下暂无可发送新闻。</section>'}
      </main>
    </body>
    </html>
    """
