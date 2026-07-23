/** Shared email list parsing + validation for mail UI and review reminders. */

const EMAIL_RE =
  /^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$/;

const EMAIL_SPLIT_RE = /[\n,;，；\s]+/;
const MAX_EMAIL_LENGTH = 254;

export function isValidEmail(value: string): boolean {
  const email = value.trim();
  if (!email || email.length > MAX_EMAIL_LENGTH) return false;
  if (email.includes("..")) return false;
  return EMAIL_RE.test(email);
}

export interface ParsedEmailList {
  rawTokens: string[];
  valid: string[];
  invalid: string[];
}

/** Split free-text recipients, keep order, de-dupe valid addresses case-insensitively. */
export function parseEmailList(text: string): ParsedEmailList {
  const rawTokens = text
    .split(EMAIL_SPLIT_RE)
    .map((token) => token.trim())
    .filter(Boolean);

  const valid: string[] = [];
  const invalid: string[] = [];
  const seen = new Set<string>();

  for (const token of rawTokens) {
    if (!isValidEmail(token)) {
      if (!invalid.includes(token)) invalid.push(token);
      continue;
    }
    const key = token.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    valid.push(token);
  }

  return { rawTokens, valid, invalid };
}

export function emailListError(
  parsed: ParsedEmailList,
  options: { requireAtLeastOne?: boolean } = {},
): string | null {
  if (parsed.invalid.length > 0) {
    const sample = parsed.invalid.slice(0, 3).join("、");
    const more = parsed.invalid.length > 3 ? ` 等 ${parsed.invalid.length} 个` : "";
    return `存在非法邮箱：${sample}${more}`;
  }
  if (options.requireAtLeastOne && parsed.valid.length === 0) {
    return "请至少填写一个有效收件人邮箱。";
  }
  return null;
}
