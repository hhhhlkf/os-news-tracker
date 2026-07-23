/** Hard caps for short UI inputs. Prompt Studio is intentionally excluded. */

export const INPUT_LIMITS = {
  /** 主分类、模板名、任务名等短名称 */
  shortName: 80,
  /** 探查别名、来源名称等展示名 */
  displayName: 120,
  /** 新闻流搜索、关键词类 */
  searchQuery: 200,
  /** 智能探查主输入（URL / 公众号 / 关键词） */
  discoveryInput: 500,
  /** 普通 URL */
  url: 2048,
  /** 单行邮箱列表 */
  emailList: 500,
  /** 多行收件人 */
  emailListMultiline: 1000,
  /** 邮件标题 */
  subject: 200,
  /** JSON path / 字段名等短表达式 */
  pathExpr: 300,
  /** 管理密码 */
  password: 128,
  /** 纯数字上限输入（如条目数） */
  countDigits: 6,
} as const;

export type InputLimitKey = keyof typeof INPUT_LIMITS;

/** Strong clamp: truncate past max (covers paste beyond maxLength in some browsers). */
export function clampInput(value: string, max: number): string {
  if (max <= 0) return "";
  return value.length <= max ? value : value.slice(0, max);
}

/** Keep only digits and clamp length — for numeric text fields. */
export function clampDigitInput(value: string, maxDigits: number = INPUT_LIMITS.countDigits): string {
  const digits = value.replace(/\D/g, "");
  return clampInput(digits, maxDigits);
}
