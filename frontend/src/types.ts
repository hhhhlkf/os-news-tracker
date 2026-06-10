export interface ItemSummary {
  id: number;
  title: string;
  title_tldr: string | null;
  main_category: string | null;
  info_type: string | null;
  importance: string | null;
  published_at: string | null;
  url: string;
}

export interface ItemDetail extends ItemSummary {
  summary: string | null;
  key_points: string[];
  why_it_matters: string | null;
  llm_confidence: number | null;
  sub_tags: string[];
  entities: { type: string; name: string }[];
  source_links: { source_id: number; url: string }[];
}

export interface FacetValue { value: string; count: number; }
export interface Facets {
  main_category: FacetValue[];
  info_type: FacetValue[];
  importance: FacetValue[];
}
export interface ItemListResponse { total: number; items: ItemSummary[]; }
