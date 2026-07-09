import type { DiscoveryRouteSource, DiscoveryRouteType } from "../types";

export const ROUTE_TYPE_LABELS: Record<DiscoveryRouteType, string> = {
  website: "网页",
  wechat_search: "微信搜索",
  wechat_history: "微信公众号",
  internal_forum: "司内论坛",
};

export interface RouteInputState {
  displayValue: string;
  value: string;
  inputPrefix: string | null;
  selectedRouteType: DiscoveryRouteType | null;
  resolvedRouteType: DiscoveryRouteType | null;
  routeSource: DiscoveryRouteSource;
  validationError: string | null;
}

const PREFIX_TO_ROUTE_TYPE: Record<string, DiscoveryRouteType> = {
  "网页": "website",
  "微信搜索": "wechat_search",
  "微信公众号": "wechat_history",
  "司内论坛": "internal_forum",
};

function splitDisplayInput(rawInput: string): {
  displayValue: string;
  inputPrefix: string | null;
  effectiveValue: string;
  prefixedRouteType: DiscoveryRouteType | null;
} {
  const displayValue = rawInput.trim();
  const match = displayValue.match(/^([^：:]+)[：:]\s*(.*)$/);
  if (!match) {
    return {
      displayValue,
      inputPrefix: null,
      effectiveValue: displayValue,
      prefixedRouteType: null,
    };
  }

  const inputPrefix = match[1].trim();
  const prefixedRouteType = PREFIX_TO_ROUTE_TYPE[inputPrefix] ?? null;
  if (!prefixedRouteType) {
    return {
      displayValue,
      inputPrefix: null,
      effectiveValue: displayValue,
      prefixedRouteType: null,
    };
  }

  const effectiveValue = match[2].trim();
  return {
    displayValue,
    inputPrefix,
    effectiveValue,
    prefixedRouteType,
  };
}

/**
 * Auto-infer route type from input content.
 * - [km] or [iwiki] markers → internal_forum
 * - URL with scheme → website (or wechat_history for mp.weixin.qq.com)
 * - Plain text → wechat_search
 */
function inferRouteType(rawInput: string): DiscoveryRouteType | null {
  const parsed = splitDisplayInput(rawInput);
  if (!parsed.displayValue) return null;
  if (parsed.prefixedRouteType) return parsed.prefixedRouteType;
  const lower = parsed.effectiveValue.toLowerCase();
  if (lower.includes("[km]") || lower.includes("[iwiki]")) return "internal_forum";
  if (lower.startsWith("http://") || lower.startsWith("https://")) {
    if (lower.includes("mp.weixin.qq.com")) return "wechat_history";
    return "website";
  }
  return "wechat_search";
}

/**
 * Validate the input value against the resolved route type.
 * Returns an error message or null if valid.
 */
function validateRouteValue(value: string, routeType: DiscoveryRouteType): string | null {
  const lower = value.toLowerCase();

  switch (routeType) {
    case "website": {
      if (!lower.startsWith("http://") && !lower.startsWith("https://")) {
        return "网页路由需要输入 http/https 开头的 URL。";
      }
      try {
        new URL(value);
      } catch {
        return "URL 格式无效。";
      }
      return null;
    }

    case "wechat_search": {
      if (lower.startsWith("http://") || lower.startsWith("https://")) {
        return "微信搜索路由不应输入 URL，请输入搜索关键词。";
      }
      return null;
    }

    case "wechat_history": {
      if (lower.startsWith("http://") || lower.startsWith("https://")) {
        if (!value.includes("mp.weixin.qq.com")) {
          return "微信公众号路由仅支持 mp.weixin.qq.com 链接或公众号名称。";
        }
        try {
          new URL(value);
        } catch {
          return "URL 格式无效。";
        }
      }
      return null;
    }

    case "internal_forum": {
      // Free text — no validation beyond non-empty (handled by caller)
      return null;
    }

    default:
      return null;
  }
}

/**
 * Single validation function that owns ALL disabled-state logic.
 * Input is free text; route type comes from dropdown or auto-inference.
 */
export function resolveDiscoveryRouteState(
  rawInput: string,
  selectedRouteType: DiscoveryRouteType | null,
): RouteInputState {
  const parsed = splitDisplayInput(rawInput);
  const trimmed = parsed.displayValue;
  const effectiveValue = parsed.effectiveValue;

  if (!trimmed) {
    return {
      displayValue: "",
      value: "",
      inputPrefix: null,
      selectedRouteType,
      resolvedRouteType: selectedRouteType,
      routeSource: selectedRouteType ? "explicit" : "inferred",
      validationError: "请输入探查内容。",
    };
  }

  // Resolve route: explicit selection wins, otherwise auto-infer
  if (selectedRouteType) {
    const validationError = validateRouteValue(effectiveValue, selectedRouteType);
    return {
      displayValue: trimmed,
      value: effectiveValue,
      inputPrefix: parsed.inputPrefix,
      selectedRouteType,
      resolvedRouteType: selectedRouteType,
      routeSource: "explicit",
      validationError,
    };
  }

  // Auto-infer
  const inferred = inferRouteType(trimmed);
  if (!inferred) {
    return {
      displayValue: trimmed,
      value: effectiveValue,
      inputPrefix: parsed.inputPrefix,
      selectedRouteType: null,
      resolvedRouteType: null,
      routeSource: "inferred",
      validationError: "无法推断路由类型，请手动选择。",
    };
  }

  const validationError = validateRouteValue(effectiveValue, inferred);
  return {
    displayValue: trimmed,
    value: effectiveValue,
    inputPrefix: parsed.inputPrefix,
    selectedRouteType: null,
    resolvedRouteType: inferred,
    routeSource: "inferred",
    validationError,
  };
}
