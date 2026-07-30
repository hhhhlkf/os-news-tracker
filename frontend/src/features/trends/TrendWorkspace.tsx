import { useEffect, useState, type CSSProperties, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createTrendIdentityTemplate,
  deleteTrendIdentityTemplate,
  fetchTrendIdentityTemplates,
  fetchTrendSettings,
  updateTrendSettings,
} from "./api";
import { TrendEmbeddingPanel } from "./TrendEmbeddingPanel";
import { TrendFlow } from "./TrendFlow";
import { TrendSettings } from "./TrendSettings";
import type { TrendSettings as TrendSettingsData } from "./types";

const templateQueryKey = ["trends", "identity-templates"] as const;
const settingsQueryKey = ["trends", "settings"] as const;
const defaultTemplateName = "新闻趋势分析师";
const defaultIdentityText = "我是新闻趋势分析师，聚焦操作系统与人工智能领域的可验证技术变化。重点识别 Linux、云原生、系统软件、基础设施、芯片与开发工具，以及大模型、Agent、推理、训练、开源生态和 AI 落地的持续演进。基于跨来源、跨主体和时间窗口证据判断新兴趋势、热点、主线与降温信号；优先关注技术成熟度、生态影响、安全与成本、兼容性和实际部署。忽略单纯营销、缺少证据的预测及孤立新闻。";

export function TrendWorkspace() {
  const queryClient = useQueryClient();
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
  const [newTemplateName, setNewTemplateName] = useState(defaultTemplateName);
  const [newIdentityText, setNewIdentityText] = useState(defaultIdentityText);
  const [templateFormOpen, setTemplateFormOpen] = useState(false);
  const [templateFormError, setTemplateFormError] = useState<string | null>(null);

  const templatesQuery = useQuery({ queryKey: templateQueryKey, queryFn: fetchTrendIdentityTemplates });
  const settingsQuery = useQuery({ queryKey: settingsQueryKey, queryFn: fetchTrendSettings });

  useEffect(() => {
    const templates = templatesQuery.data ?? [];
    if (selectedTemplateId && templates.some((template) => template.template_id === selectedTemplateId)) return;
    setSelectedTemplateId(settingsQuery.data?.scheduled_template_id ?? templates[0]?.template_id ?? null);
  }, [selectedTemplateId, settingsQuery.data?.scheduled_template_id, templatesQuery.data]);

  const createTemplateMutation = useMutation({
    mutationFn: createTrendIdentityTemplate,
    onSuccess: async (template) => {
      setSelectedTemplateId(template.template_id);
      setNewTemplateName(defaultTemplateName);
      setNewIdentityText(defaultIdentityText);
      setTemplateFormOpen(false);
      await queryClient.invalidateQueries({ queryKey: templateQueryKey });
    },
  });
  const deleteTemplateMutation = useMutation({
    mutationFn: deleteTrendIdentityTemplate,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: templateQueryKey }),
        queryClient.invalidateQueries({ queryKey: settingsQueryKey }),
      ]);
    },
  });
  const saveSettingsMutation = useMutation({
    mutationFn: updateTrendSettings,
    onSuccess: async (settings) => {
      queryClient.setQueryData(settingsQueryKey, settings);
      await queryClient.invalidateQueries({ queryKey: ["trends", "cards"] });
    },
  });

  const templates = templatesQuery.data ?? [];
  const selectedTemplate = templates.find((template) => template.template_id === selectedTemplateId) ?? null;
  const templateError = templatesQuery.error ?? createTemplateMutation.error ?? deleteTemplateMutation.error;

  async function handleCreateTemplate(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setTemplateFormError(null);
    if (!newTemplateName.trim()) {
      setTemplateFormError("请填写模板名称。");
      return;
    }
    if (!newIdentityText.trim()) {
      setTemplateFormError("请填写身份文本。");
      return;
    }
    await createTemplateMutation.mutateAsync({ name: newTemplateName.trim(), identity_text: newIdentityText.trim() });
  }

  async function handleDeleteTemplate(templateId: string): Promise<void> {
    const template = templates.find((item) => item.template_id === templateId);
    if (!window.confirm(`删除身份模板「${template?.name ?? ""}」？这只会清除它后续关联的趋势结果，不会影响全局新闻事实。`)) return;
    await deleteTemplateMutation.mutateAsync(templateId);
  }

  async function handleSaveSettings(settings: TrendSettingsData): Promise<void> {
    await saveSettingsMutation.mutateAsync(settings);
  }

  return (
    <section style={workspace} aria-labelledby="trend-workspace-title">
      <header style={workspaceHeader}>
        <div>
          <div style={eyebrow}>News Trend Summary Workspace</div>
          <h1 id="trend-workspace-title" style={title}>新闻趋势总结工作台</h1>
          <p style={intro}>用身份模板定义观察视角，统一保存趋势窗口与后续运行规则；新闻解释卡可按当前范围独立补齐。</p>
        </div>
      </header>

      <div style={layout}>
        <section style={panel}>
          <div style={panelHeader}>
            <div>
              <div style={sectionTitle}>身份模板</div>
              <div style={sectionCopy}>模板创建后不可编辑；如需调整身份视角，请新建一个模板。</div>
            </div>
            <button type="button" style={secondaryButton} onClick={() => { setTemplateFormError(null); setTemplateFormOpen((open) => !open); }}>
              {templateFormOpen ? "收起新建" : "新建模板"}
            </button>
          </div>

          {templateFormOpen && (
            <form onSubmit={(event) => void handleCreateTemplate(event)} style={createForm}>
              <label style={field}>
                <span style={labelText}>模板名称</span>
                <input value={newTemplateName} onChange={(event) => setNewTemplateName(event.target.value)} placeholder="例如：企业技术战略负责人" style={input} disabled={createTemplateMutation.isPending} />
              </label>
              <label style={field}>
                <span style={labelText}>身份文本</span>
                <textarea
                  value={newIdentityText}
                  onChange={(event) => setNewIdentityText(event.target.value)}
                  placeholder="描述身份、职责、关注方向、判断标准与希望规避的偏差。"
                  maxLength={300}
                  rows={5}
                  style={textarea}
                  disabled={createTemplateMutation.isPending}
                />
                <span style={counter}>{newIdentityText.length}/300</span>
              </label>
              {templateFormError && <div style={errorBox}>{templateFormError}</div>}
              {createTemplateMutation.error && <div style={errorBox}>{createTemplateMutation.error.message}</div>}
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button type="submit" style={primaryButton} disabled={createTemplateMutation.isPending}>
                  {createTemplateMutation.isPending ? "创建中…" : "创建身份模板"}
                </button>
              </div>
            </form>
          )}

          {templatesQuery.isLoading && <div style={empty}>正在加载身份模板…</div>}
          {!templatesQuery.isLoading && templates.length === 0 && (
            <div style={emptyState}>
              <div style={emptyTitle}>尚未创建身份模板</div>
              <div>先新建一个身份模板，为后续趋势总结提供稳定的观察视角。</div>
            </div>
          )}
          {templateError && !createTemplateMutation.error && <div style={errorBox}>{templateError.message}</div>}
          {templates.length > 0 && (
            <div style={templateList}>
              {templates.map((template) => {
                const selected = template.template_id === selectedTemplateId;
                return (
                  <article key={template.template_id} style={selected ? selectedTemplateCard : templateCard}>
                    <button type="button" style={templateSelectButton} onClick={() => setSelectedTemplateId(template.template_id)} aria-pressed={selected}>
                      <span style={templateName}>{template.name}</span>
                      <span style={templateDate}>创建于 {template.created_at}</span>
                      <span style={templateText}>{template.identity_text}</span>
                    </button>
                    <button
                      type="button"
                      style={deleteButton}
                      onClick={() => void handleDeleteTemplate(template.template_id)}
                      disabled={deleteTemplateMutation.isPending}
                      aria-label={`删除模板 ${template.name}`}
                    >
                      删除
                    </button>
                  </article>
                );
              })}
            </div>
          )}
          {selectedTemplate && (
            <div style={selectedHint}>
              当前工作台视角：<strong>{selectedTemplate.name}</strong>
              。第 4 步会对该模板手动运行趋势总结，并只在完整成功后发布最新结果。
            </div>
          )}
        </section>

        <TrendSettings
          settings={settingsQuery.data}
          templates={templates}
          isLoading={settingsQuery.isLoading}
          isSaving={saveSettingsMutation.isPending}
          error={(settingsQuery.error ?? saveSettingsMutation.error) as Error | null}
          onSave={handleSaveSettings}
        />
      </div>

      <TrendEmbeddingPanel />

      <TrendFlow selectedTemplateId={selectedTemplateId} />
    </section>
  );
}

const workspace: CSSProperties = {};
const workspaceHeader: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 18, minHeight: 160, marginBottom: 16, flexWrap: "wrap", background: "linear-gradient(135deg, #0f172a 0%, #101828 55%, #1d2939 100%)", color: "#f8fafc", borderRadius: 8, padding: 24, boxShadow: "0 18px 40px rgba(15, 23, 42, 0.14)" };
const eyebrow: CSSProperties = { color: "#98a2b3", fontSize: 13, marginBottom: 10 };
const title: CSSProperties = { color: "#f8fafc", fontSize: 32, margin: 0, lineHeight: 1.2 };
const intro: CSSProperties = { color: "#d0d5dd", fontSize: 14, margin: "10px 0 0", maxWidth: 760 };
const layout: CSSProperties = { display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 14, marginBottom: 14 };
const panel: CSSProperties = { border: "1px solid #eaecf0", borderRadius: 12, padding: 20, background: "#fff", boxShadow: "0 1px 2px rgba(16,24,40,.04)" };
const panelHeader: CSSProperties = { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, marginBottom: 16 };
const sectionTitle: CSSProperties = { color: "#101828", fontSize: 16, fontWeight: 800 };
const sectionCopy: CSSProperties = { color: "#667085", fontSize: 12, marginTop: 5, lineHeight: 1.6 };
const primaryButton: CSSProperties = { border: "1px solid #175cd3", borderRadius: 8, background: "#175cd3", color: "#fff", padding: "8px 12px", fontSize: 12, fontWeight: 800, cursor: "pointer" };
const secondaryButton: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 8, background: "#fff", color: "#344054", padding: "8px 10px", fontSize: 12, fontWeight: 800, cursor: "pointer", whiteSpace: "nowrap" };
const createForm: CSSProperties = { display: "grid", gap: 12, padding: 14, marginBottom: 14, border: "1px solid #dbeafe", borderRadius: 10, background: "#f8fbff" };
const field: CSSProperties = { display: "grid", gap: 6 };
const labelText: CSSProperties = { color: "#344054", fontSize: 12, fontWeight: 700 };
const input: CSSProperties = { width: "100%", boxSizing: "border-box", border: "1px solid #d0d5dd", borderRadius: 8, padding: "8px 10px", color: "#344054", background: "#fff", fontSize: 13 };
const textarea: CSSProperties = { ...input, resize: "vertical", fontFamily: "inherit", lineHeight: 1.55 };
const counter: CSSProperties = { color: "#98a2b3", fontSize: 11, textAlign: "right" };
const templateList: CSSProperties = { display: "grid", gap: 9 };
const templateCard: CSSProperties = { display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: 10, border: "1px solid #eaecf0", borderRadius: 10, padding: 12, background: "#fff" };
const selectedTemplateCard: CSSProperties = { ...templateCard, borderColor: "#84adff", background: "#f8fbff", boxShadow: "0 0 0 2px rgba(23,92,211,.08)" };
const templateSelectButton: CSSProperties = { border: 0, padding: 0, background: "transparent", textAlign: "left", cursor: "pointer", display: "grid", gap: 4, minWidth: 0 };
const templateName: CSSProperties = { color: "#101828", fontSize: 13, fontWeight: 800 };
const templateDate: CSSProperties = { color: "#98a2b3", fontSize: 11 };
const templateText: CSSProperties = { color: "#667085", fontSize: 12, lineHeight: 1.55, marginTop: 3 };
const deleteButton: CSSProperties = { alignSelf: "start", border: 0, background: "transparent", color: "#b42318", fontSize: 12, fontWeight: 700, padding: 3, cursor: "pointer" };
const empty: CSSProperties = { color: "#98a2b3", textAlign: "center", padding: 28, fontSize: 13 };
const emptyState: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 10, color: "#667085", textAlign: "center", padding: "28px 20px", fontSize: 13, lineHeight: 1.65 };
const emptyTitle: CSSProperties = { color: "#344054", fontWeight: 800, marginBottom: 5 };
const selectedHint: CSSProperties = { marginTop: 12, color: "#475467", fontSize: 12, lineHeight: 1.6 };
const errorBox: CSSProperties = { border: "1px solid #fecdca", borderRadius: 8, background: "#fef3f2", color: "#b42318", padding: "8px 10px", fontSize: 12 };
