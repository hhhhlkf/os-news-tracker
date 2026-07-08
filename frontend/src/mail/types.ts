export type MailSortBy = "published_at" | "fetched_at";
export type MailSortDir = "desc" | "asc";
export type MailBoundaryMode = "none" | "absolute" | "relative";
export type MailRelativeRange = "24h" | "7d" | "30d";

export interface MailFilterSnapshot {
  q?: string | null;
  main_category?: string | null;
  info_type?: string | null;
  importance?: string | null;
  sub_tag?: string | null;
  sort_by?: MailSortBy;
  sort_dir?: MailSortDir;
  published_after_mode?: MailBoundaryMode;
  published_after_value?: MailRelativeRange | null;
  published_after?: string | null;
  published_before_mode?: MailBoundaryMode;
  published_before_value?: MailRelativeRange | null;
  published_before?: string | null;
}

export interface MailTemplate {
  id: number;
  name: string;
  subject: string;
  recipients: string[];
  filter_snapshot: MailFilterSnapshot;
  is_active: boolean;
  last_send_at: string | null;
  last_send_status: string | null;
  last_send_count: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MailTemplateCreateRequest {
  name: string;
  subject: string;
  recipients: string[];
  filter_snapshot: MailFilterSnapshot;
  is_active?: boolean;
}

export interface MailPreviewItem {
  title: string;
  reason: string | null;
  summary: string | null;
  key_points: string[];
  hotspots: string[];
  source_url: string;
  published_at: string | null;
}

export interface MailPreviewResponse {
  subject: string;
  filter_snapshot: MailFilterSnapshot;
  recipients: string[];
  item_count: number;
  items: MailPreviewItem[];
  rendered_html: string;
}

export interface MailImmediateSendRequest {
  subject: string;
  recipients: string[];
  filter_snapshot: MailFilterSnapshot;
}

export interface MailImmediateSendResponse {
  delivery_id: number;
  status: string;
  item_count: number;
  error_message: string | null;
}
