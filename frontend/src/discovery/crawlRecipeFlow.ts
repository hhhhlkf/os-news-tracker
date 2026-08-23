export interface CrawlFlowStep {
  icon: string;
  title: string;
  detail?: string;
  tags?: string[];
  children?: CrawlFlowStep[];
}

type Action = Record<string, unknown>;

const asActionList = (value: unknown): Action[] => Array.isArray(value)
  ? value.filter((item): item is Action => typeof item === "object" && item !== null)
  : [];

const text = (value: unknown): string | undefined => typeof value === "string" && value.trim() ? value : undefined;

function displayUrl(value: unknown): string | undefined {
  const url = text(value);
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return `${parsed.host}${parsed.pathname}${parsed.search}`;
  } catch {
    return url;
  }
}

function conditionDescription(value: unknown): string | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const condition = value as Record<string, unknown>;
  if (text(condition.not_exists)) return `直到页面元素 ${condition.not_exists} 消失`;
  if (text(condition.exists)) return `直到页面出现 ${condition.exists}`;
  const target = text(condition.count_of) ?? text(condition.var) ?? text(condition.path);
  if (!target) return undefined;
  const operator = text(condition.op) ?? "=";
  const expected = condition.value === undefined ? "" : ` ${String(condition.value)}`;
  return `直到 ${target} ${operator}${expected}`;
}

function fieldsDescription(value: unknown): string | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const fields = Object.keys(value);
  return fields.length ? `提取字段：${fields.join("、")}` : undefined;
}

function describeAction(action: Action): CrawlFlowStep {
  const op = text(action.op) ?? "unknown";
  switch (op) {
    case "fetch": {
      const method = text(action.method) ?? "GET";
      const mode = text(action.mode)?.toUpperCase();
      return {
        icon: "↓", title: "请求数据", detail: displayUrl(action.url),
        tags: [method, mode, text(action.transport) === "scrapling" ? "反爬浏览器" : "HTTP"].filter(Boolean) as string[],
      };
    }
    case "goto":
      return { icon: "↗", title: "打开网页", detail: displayUrl(action.url), tags: [text(action.wait_until) ?? "networkidle"] };
    case "wait_for":
      return { icon: "◌", title: "等待页面就绪", detail: text(action.selector), tags: [action.timeout_ms ? `${action.timeout_ms} ms` : ""].filter(Boolean) as string[] };
    case "click":
      return { icon: "↳", title: "点击页面元素", detail: text(action.selector), tags: [action.after_wait_ms ? `等待 ${action.after_wait_ms} ms` : ""].filter(Boolean) as string[] };
    case "extract":
      return { icon: "⊞", title: "提取新闻条目", detail: fieldsDescription(action.fields), tags: [text(action.from), action.merge ? "追加结果" : "写入结果"].filter(Boolean) as string[] };
    case "set":
      return { icon: "=", title: "设置分页变量", detail: `${text(action.var) ?? "变量"} = ${text(action.expr) ?? String(action.value ?? "")}` };
    case "dedup_by":
      return { icon: "≡", title: "去重新闻条目", detail: `按 ${text(action.field) ?? "字段"} 去重` };
    case "enrich_article_pages":
      return { icon: "▤", title: "补抓文章详情页", detail: "补齐正文、摘要等缺失内容", tags: enrichmentTags(action) };
    case "enrich_article_api":
      return { icon: "▤", title: "通过详情 API 补抓内容", detail: displayUrl(action.url_template), tags: enrichmentTags(action) };
    case "enrich_wechat_articles":
      return { icon: "▤", title: "补抓微信文章详情", detail: "补齐发布时间、摘要和正文", tags: enrichmentTags(action) };
    case "wechat_search_articles":
      return { icon: "⌕", title: "搜索微信公众号文章", detail: `关键词：${text(action.query) ?? ""}`, tags: pageTags(action) };
    case "wechat_fetch_account_history":
      return { icon: "⌕", title: "拉取公众号历史文章", detail: text(action.nickname) ?? text(action.account_id), tags: [action.fetch_content ? "同时抓取正文" : "文章列表", action.limit ? `最多 ${action.limit} 篇` : ""].filter(Boolean) as string[] };
    case "mcp_call":
      return { icon: "◇", title: "调用内部数据工具", detail: [text(action.server), text(action.tool)].filter(Boolean).join(" · ") };
    case "loop": {
      const children = [...asActionList(action.body), ...asActionList(action.on_each)].map(describeAction);
      return {
        icon: "↻", title: "循环翻页", detail: conditionDescription(action.until),
        tags: [action.max_iters ? `最多 ${action.max_iters} 轮` : ""].filter(Boolean) as string[], children,
      };
    }
    default:
      return { icon: "•", title: "执行扩展步骤", detail: op };
  }
}

function enrichmentTags(action: Action): string[] {
  return [action.fill_missing_only ? "仅补齐缺失字段" : "覆盖字段", action.max_items ? `最多 ${action.max_items} 篇` : ""]
    .filter(Boolean) as string[];
}

function pageTags(action: Action): string[] {
  return [action.max_pages ? `最多 ${action.max_pages} 页` : "", action.limit ? `最多 ${action.limit} 篇` : ""]
    .filter(Boolean) as string[];
}

export function buildCrawlFlow(recipe: Record<string, unknown>): CrawlFlowStep[] {
  return asActionList(recipe.actions).map(describeAction);
}
