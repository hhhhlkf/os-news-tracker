import { useEffect, type CSSProperties, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * PROTOTYPE — three variants of the technical-architecture diagram, switchable
 * with /prototype/architecture?variant=a|b|c. This answers: which visual
 * hierarchy best explains the product architecture in the technical document?
 */

type VariantKey = "a" | "b" | "c";

const variants: Array<{ key: VariantKey; name: string }> = [
  { key: "a", name: "分层模块图" },
  { key: "b", name: "Item 中心图" },
  { key: "c", name: "运行域全景图" },
];

export function ArchitectureDiagramPrototype() {
  const [searchParams, setSearchParams] = useSearchParams();
  const rawVariant = searchParams.get("variant");
  const variant: VariantKey = rawVariant === "b" || rawVariant === "c" ? rawVariant : "a";
  const selected = variants.find((candidate) => candidate.key === variant) ?? variants[0];

  const changeVariant = (next: VariantKey) => setSearchParams({ variant: next });
  const cycle = (direction: -1 | 1) => {
    const index = variants.findIndex((candidate) => candidate.key === variant);
    changeVariant(variants[(index + direction + variants.length) % variants.length].key);
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const element = event.target as HTMLElement | null;
      if (element?.matches("input, textarea, [contenteditable='true']")) return;
      if (event.key === "ArrowLeft") cycle(-1);
      if (event.key === "ArrowRight") cycle(1);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  return (
    <main style={page}>
      {variant === "a" && <LayeredArchitecture />}
      {variant === "b" && <ItemCentricArchitecture />}
      {variant === "c" && <OperationsArchitecture />}
      {import.meta.env.DEV && <PrototypeSwitcher selected={selected} onCycle={cycle} />}
    </main>
  );
}

function DiagramHeader({ eyebrow, title, children }: { eyebrow: string; title: string; children: ReactNode }) {
  return <header style={header}>
    <div style={eyebrowStyle}>{eyebrow}</div>
    <h1 style={titleStyle}>{title}</h1>
    <p style={subtitle}>{children}</p>
  </header>;
}

function LayeredArchitecture() {
  return <div style={canvas}>
    <DiagramHeader eyebrow="OS NEWS TRACKER · ARCHITECTURE" title="技术情报平台总体架构">
      以统一 Item 为数据底座：上层接入不同情报来源，中层完成探查、查取与整理，下层负责趋势、分发和稳定运行。
    </DiagramHeader>

    <ArchitectureBand color="warm" label="信息入口层" caption="公开网站、技术讨论与任务触发">
      <ModuleCard icon="◫" title="常规新闻源" lines={["RSS · 页面监测", "关键词搜索 · API"]} />
      <ModuleCard icon="✉" title="技术邮件列表" lines={["IMAP 增量收取", "Header 规则匹配"]} />
      <ModuleCard icon="◆" title="GitHub 仓库" lines={["Issue · Discussion", "评论与更新时间水位"]} />
      <ModuleCard icon="◷" title="任务入口" lines={["手动执行", "定时与巡检触发"]} />
    </ArchitectureBand>

    <DownArrow />

    <ArchitectureBand color="blue" label="探查与查取层" caption="受控生成、审核、隔离执行">
      <ModuleCard icon="⌕" title="智能探查" lines={["Context · Explore · Build", "Execute · Evaluate · Package"]} emphasis />
      <ModuleCard icon="✓" title="Connector 审核与版本" lines={["Manifest · 校验和", "待审核 → 已启用"]} />
      <ModuleCard icon="⇣" title="正式 Connector 查取" lines={["gVisor 沙箱执行", "可取消 · 单方法隔离"]} />
      <ModuleCard icon="⌘" title="技术讨论整理" lines={["重建讨论树与候选组", "结构化总结与发布"]} />
    </ArchitectureBand>

    <DownArrow />

    <ArchitectureBand color="cyan" label="统一情报处理层" caption="所有入口收敛为可追溯的 Item">
      <ModuleCard wide icon="▣" title="CrawlOutputIngester 与新闻 Pipeline" lines={["normalize · relevance-filter · dedup · enrich", "来源关联、标签实体、重要性与幂等入库"]} />
      <ModuleCard wide icon="●" title="统一 Item 情报库" lines={["网页新闻、邮件讨论、GitHub 技术讨论共享同一检索与分析模型", "保留来源、证据消息、运行记录与时间边界"]} emphasis />
    </ArchitectureBand>

    <DownArrow />

    <ArchitectureBand color="green" label="分析与分发层" caption="从统一事实到可消费情报">
      <ModuleCard icon="▤" title="新闻流与详情" lines={["筛选 · 排序 · 来源追溯", "讨论回复拓扑"]} />
      <ModuleCard icon="⌁" title="趋势分析" lines={["解释卡 · 向量 · 聚类", "故事线 · 身份模板趋势"]} emphasis />
      <ModuleCard icon="✉" title="邮件分发" lines={["模板预览与即时发送", "趋势分发与预定任务"]} />
      <ModuleCard icon="◴" title="统计与运营" lines={["运行状态 · 用量 · 入库量", "审核提醒与发送日志"]} />
    </ArchitectureBand>

    <ArchitectureBand color="slate" label="平台运行底座" caption="为所有上层模块提供稳定、受控的执行环境">
      <ModuleCard compact title="FastAPI 与访问控制" lines={["路由 · 输入约束 · 状态事件"]} />
      <ModuleCard compact title="任务调度器" lines={["新闻源 · 系统查取 · 趋势 · 邮件"]} />
      <ModuleCard compact title="Sandbox Runtime" lines={["Docker + gVisor · 网络与容量控制"]} />
      <ModuleCard compact title="PostgreSQL 与制品目录" lines={["业务数据 · 运行记录 · 版本化 Connector"]} />
      <ModuleCard compact title="LLM 与 Embedding" lines={["探查、讨论整理与趋势计算"]} />
    </ArchitectureBand>
  </div>;
}

function ItemCentricArchitecture() {
  return <div style={canvas}>
    <DiagramHeader eyebrow="VARIANT B · DATA-CENTRIC" title="以 Item 为中心的情报架构">
      这版突出“邮件、GitHub 与网页最终进入同一情报模型”的关系，适合说明数据汇合与下游复用。
    </DiagramHeader>
    <section style={hubLayout}>
      <div style={sourceColumn}>
        <SectionTag>情报来源</SectionTag>
        <ModuleCard icon="◫" title="常规新闻源" lines={["RSS · 页面 · 搜索 · API"]} />
        <ModuleCard icon="⌕" title="站点 Connector" lines={["探查生成 · 审核发布", "gVisor 正式查取"]} />
        <ModuleCard icon="✉" title="技术邮件" lines={["IMAP · 规则 · 邮件树"]} />
        <ModuleCard icon="◆" title="GitHub 讨论" lines={["Issue · Discussion · 评论"]} />
      </div>
      <div style={hubCenter}>
        <div style={inboundLabel}>统一处理与整理</div>
        <div style={itemHalo}>
          <div style={itemCore}><span style={itemIcon}>●</span><b>Item 情报库</b><small>统一条目、来源、证据与时间范围</small></div>
        </div>
        <div style={pipelineStrip}><b>新闻 Pipeline</b><span>规范化</span><span>相关性</span><span>去重</span><span>富化</span><span>幂等存储</span></div>
        <div style={discussionStrip}><b>讨论整理器</b><span>候选组</span><span>结构化总结</span><span>发布 Item</span></div>
      </div>
      <div style={consumerColumn}>
        <SectionTag>情报消费</SectionTag>
        <ModuleCard icon="▤" title="新闻流" lines={["检索、筛选、详情"]} />
        <ModuleCard icon="⌁" title="趋势工作台" lines={["解释卡、聚类、故事线", "模板化趋势结果"]} emphasis />
        <ModuleCard icon="✉" title="邮件任务中心" lines={["预览、即时发送、预定分发"]} />
        <ModuleCard icon="◴" title="统计与审计" lines={["运行量、用量、日志与提醒"]} />
      </div>
    </section>
    <section style={foundationPanel}>
      <SectionTag>共同运行底座</SectionTag>
      <div style={foundationCards}>
        <ModuleCard compact title="FastAPI / React" lines={["访问控制、页面与实时状态"]} />
        <ModuleCard compact title="调度与任务控制" lines={["定时、巡检、取消与恢复"]} />
        <ModuleCard compact title="gVisor Sandbox" lines={["Connector 隔离执行与网络策略"]} />
        <ModuleCard compact title="PostgreSQL" lines={["数据、运行记录和审核状态"]} />
      </div>
    </section>
  </div>;
}

function OperationsArchitecture() {
  return <div style={canvas}>
    <DiagramHeader eyebrow="VARIANT C · OPERATING DOMAINS" title="技术情报平台运行域全景">
      这版按“管理、执行、数据、消费”划分模块，强调任务所有权、运行状态和平台治理能力。
    </DiagramHeader>
    <section style={operationsGrid}>
      <div style={domainColumn}>
        <DomainHeader number="01" title="配置与治理" text="决定允许收什么、如何收、谁可以执行。" />
        <ModuleCard title="来源与方法库" lines={["新闻来源 · 邮件规则 · GitHub 仓库", "Connector 方法、审核与启停"]} />
        <ModuleCard title="运行策略" lines={["时间范围、条数、Cron、巡检", "审核提醒与容量优先级"]} />
      </div>
      <div style={domainColumn}>
        <DomainHeader number="02" title="智能探查与执行" text="把未知站点转为可验证、可运行的能力。" />
        <ModuleCard title="Single Agent Loop" lines={["探查证据 · 构建制品 · 确定性验收"]} emphasis />
        <ModuleCard title="正式查取与技术讨论" lines={["Connector 沙箱任务", "邮件/GitHub 收取、整理与停止"]} />
      </div>
      <div style={domainColumn}>
        <DomainHeader number="03" title="统一事实层" text="让不同入口的内容可以同样被查询和复用。" />
        <ModuleCard title="处理与存储" lines={["新闻 Pipeline · DiscussionOrganizer", "Item、来源关联、证据与运行记录"]} emphasis />
        <ModuleCard title="质量控制" lines={["去重、相关性、富化、审核状态", "失败、取消、部分成功与恢复"]} />
      </div>
      <div style={domainColumn}>
        <DomainHeader number="04" title="分析与触达" text="把事实组织成新闻、趋势和可发送的信息。" />
        <ModuleCard title="趋势工作台" lines={["解释卡、向量、聚类、故事线", "身份模板、趋势轮播与定时运行"]} />
        <ModuleCard title="邮件与前端体验" lines={["模板、预览、发送、投递日志", "新闻流、任务面板、统计图表"]} />
      </div>
    </section>
    <section style={runtimeRail}>
      <div><b>平台支撑</b><span>FastAPI</span><span>PostgreSQL</span><span>APScheduler</span><span>Docker + gVisor</span><span>LLM</span><span>Embedding Worker</span></div>
      <small>所有运行域统一使用访问控制、状态事件、日志脱敏和可追溯记录。</small>
    </section>
  </div>;
}

function ArchitectureBand({ color, label, caption, children }: { color: BandColor; label: string; caption: string; children: ReactNode }) {
  return <section style={{ ...band, ...bandColor[color] }}>
    <div style={bandMeta}><div style={bandLabel}>{label}</div><div style={bandCaption}>{caption}</div></div>
    <div style={bandContent}>{children}</div>
  </section>;
}

type BandColor = "warm" | "blue" | "cyan" | "green" | "slate";
const bandColor: Record<BandColor, CSSProperties> = {
  warm: { background: "linear-gradient(135deg, #fff6d8, #fffaf0)", borderColor: "#f0d38b" },
  blue: { background: "linear-gradient(135deg, #e4f1ff, #f1f7ff)", borderColor: "#b2d5f8" },
  cyan: { background: "linear-gradient(135deg, #ddf7fb, #ecfbff)", borderColor: "#9edfe9" },
  green: { background: "linear-gradient(135deg, #e8f8de, #f5fff0)", borderColor: "#b8dfa6" },
  slate: { background: "linear-gradient(135deg, #edf1f7, #f8fafc)", borderColor: "#cdd7e6" },
};

function ModuleCard({ icon, title, lines, emphasis = false, wide = false, compact = false }: { icon?: string; title: string; lines: string[]; emphasis?: boolean; wide?: boolean; compact?: boolean }) {
  return <article style={{ ...moduleCard, ...(emphasis ? moduleEmphasis : {}), ...(wide ? wideCard : {}), ...(compact ? compactCard : {}) }}>
    {icon && <span style={{ ...moduleIcon, ...(emphasis ? moduleIconEmphasis : {}) }}>{icon}</span>}
    <div><h2 style={{ ...moduleTitle, ...(compact ? compactTitle : {}) }}>{title}</h2>{lines.map((line) => <p key={line} style={{ ...moduleLine, ...(compact ? compactLine : {}) }}>{line}</p>)}</div>
  </article>;
}

function DownArrow() { return <div style={downArrow} aria-hidden="true">↓</div>; }
function SectionTag({ children }: { children: ReactNode }) { return <div style={sectionTag}>{children}</div>; }
function DomainHeader({ number, title, text }: { number: string; title: string; text: string }) { return <div style={domainHeader}><span>{number}</span><div><b>{title}</b><p>{text}</p></div></div>; }

function PrototypeSwitcher({ selected, onCycle }: { selected: { key: VariantKey; name: string }; onCycle: (direction: -1 | 1) => void }) {
  return <div style={switcher}>
    <button type="button" style={switcherButton} onClick={() => onCycle(-1)} aria-label="查看上一版">←</button>
    <span style={switcherLabel}>原型 {selected.key.toUpperCase()} · {selected.name}</span>
    <button type="button" style={switcherButton} onClick={() => onCycle(1)} aria-label="查看下一版">→</button>
  </div>;
}

const page: CSSProperties = { minHeight: "100vh", padding: "36px 24px 80px", background: "#f2f5f8", boxSizing: "border-box", color: "#14243a", fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" };
const canvas: CSSProperties = { maxWidth: 1280, margin: "0 auto", padding: "38px", borderRadius: 28, background: "#fff", boxShadow: "0 20px 60px rgba(27, 48, 81, .14)", overflow: "hidden" };
const header: CSSProperties = { padding: "0 8px 28px", textAlign: "center" };
const eyebrowStyle: CSSProperties = { color: "#3379c7", fontSize: 12, letterSpacing: ".14em", fontWeight: 800 };
const titleStyle: CSSProperties = { margin: "9px 0 10px", color: "#172c47", fontSize: 34, letterSpacing: ".02em" };
const subtitle: CSSProperties = { margin: "0 auto", maxWidth: 760, color: "#5c6c81", fontSize: 15, lineHeight: 1.7 };
const band: CSSProperties = { position: "relative", display: "grid", gridTemplateColumns: "160px minmax(0, 1fr)", gap: 22, marginTop: 14, padding: "23px 24px", border: "1px solid", borderRadius: 24 };
const bandMeta: CSSProperties = { display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", textAlign: "center" };
const bandLabel: CSSProperties = { minWidth: 112, padding: "10px 14px", borderRadius: 10, color: "#fff", background: "linear-gradient(135deg, #418ad7, #2a6db8)", fontSize: 16, fontWeight: 800, boxShadow: "0 7px 15px rgba(40, 105, 177, .18)" };
const bandCaption: CSSProperties = { marginTop: 10, color: "#52647a", fontSize: 12, lineHeight: 1.55 };
const bandContent: CSSProperties = { display: "flex", alignItems: "stretch", justifyContent: "center", gap: 14, flexWrap: "wrap" };
const moduleCard: CSSProperties = { flex: "1 1 170px", minWidth: 0, display: "flex", gap: 11, alignItems: "flex-start", padding: "15px", border: "1.5px solid #315273", borderRadius: 15, background: "rgba(255,255,255,.92)", boxShadow: "0 5px 12px rgba(39, 67, 100, .08)" };
const moduleEmphasis: CSSProperties = { borderColor: "#1775c5", boxShadow: "0 0 0 3px rgba(23, 117, 197, .10), 0 7px 16px rgba(39, 91, 145, .13)" };
const wideCard: CSSProperties = { flexBasis: "310px" };
const compactCard: CSSProperties = { flexBasis: "150px", padding: "12px", borderRadius: 12 };
const moduleIcon: CSSProperties = { flex: "0 0 auto", display: "inline-flex", width: 26, height: 26, alignItems: "center", justifyContent: "center", borderRadius: 7, color: "#2465a5", background: "#eaf3fd", fontSize: 17, fontWeight: 900 };
const moduleIconEmphasis: CSSProperties = { color: "#fff", background: "#287dcc" };
const moduleTitle: CSSProperties = { margin: 0, color: "#1f3651", fontSize: 15, lineHeight: 1.35, fontWeight: 800 };
const compactTitle: CSSProperties = { fontSize: 13 };
const moduleLine: CSSProperties = { margin: "4px 0 0", color: "#556981", fontSize: 12, lineHeight: 1.48 };
const compactLine: CSSProperties = { fontSize: 11 };
const downArrow: CSSProperties = { position: "relative", zIndex: 2, width: 38, height: 34, margin: "-3px 0 -10px 211px", display: "grid", placeItems: "center", borderRadius: "0 0 12px 12px", color: "#2d709f", background: "transparent", fontSize: 30, fontWeight: 900, lineHeight: 1 };
const hubLayout: CSSProperties = { display: "grid", gridTemplateColumns: "minmax(200px, .85fr) minmax(360px, 1.2fr) minmax(200px, .85fr)", gap: 24, alignItems: "stretch" };
const sourceColumn: CSSProperties = { display: "grid", gap: 12, alignContent: "start", padding: 18, borderRadius: 20, background: "#fff8e7" };
const consumerColumn: CSSProperties = { display: "grid", gap: 12, alignContent: "start", padding: 18, borderRadius: 20, background: "#ebf8ec" };
const sectionTag: CSSProperties = { color: "#2f6fb6", fontSize: 13, fontWeight: 800, letterSpacing: ".08em", textTransform: "uppercase" };
const hubCenter: CSSProperties = { display: "flex", flexDirection: "column", justifyContent: "center", padding: "18px 12px", border: "1px dashed #9bb7d7", borderRadius: 20, background: "linear-gradient(135deg, #edf6ff, #fbfdff)" };
const inboundLabel: CSSProperties = { color: "#54708e", fontSize: 12, fontWeight: 700, textAlign: "center" };
const itemHalo: CSSProperties = { display: "grid", placeItems: "center", width: "min(290px, 100%)", aspectRatio: "1", margin: "12px auto", borderRadius: "50%", background: "radial-gradient(circle, #fff 34%, #d6ecff 35%, #eaf6ff 55%, #c6e5ff 56%, transparent 57%)" };
const itemCore: CSSProperties = { display: "grid", justifyItems: "center", gap: 6, width: 148, height: 148, padding: 14, boxSizing: "border-box", border: "3px solid #2e7ac3", borderRadius: "50%", color: "#1f609f", background: "#fff", textAlign: "center", boxShadow: "0 8px 20px rgba(41,113,183,.18)" };
const itemIcon: CSSProperties = { fontSize: 27, lineHeight: 1 };
const pipelineStrip: CSSProperties = { display: "flex", justifyContent: "center", gap: 7, flexWrap: "wrap", padding: 11, borderRadius: 12, color: "#275780", background: "#dcefff", fontSize: 11 };
const discussionStrip: CSSProperties = { ...pipelineStrip, marginTop: 9, color: "#267050", background: "#ddf6e7" };
const foundationPanel: CSSProperties = { marginTop: 26, padding: 20, border: "1px solid #d7e1ec", borderRadius: 20, background: "#f6f9fc" };
const foundationCards: CSSProperties = { display: "flex", gap: 12, marginTop: 13, flexWrap: "wrap" };
const operationsGrid: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 14, alignItems: "stretch" };
const domainColumn: CSSProperties = { display: "grid", alignContent: "start", gap: 12, padding: 16, border: "1px solid #d2dfeb", borderRadius: 18, background: "#fbfdff" };
const domainHeader: CSSProperties = { display: "flex", gap: 10, minHeight: 84, paddingBottom: 11, borderBottom: "1px solid #dce6ef" };
const runtimeRail: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 20, alignItems: "center", marginTop: 18, padding: "18px 20px", borderRadius: 16, color: "#fff", background: "linear-gradient(135deg, #203c5b, #315f89)" };
const switcher: CSSProperties = { position: "fixed", zIndex: 10, bottom: 18, left: "50%", transform: "translateX(-50%)", display: "flex", alignItems: "center", gap: 8, padding: 7, border: "1px solid #1d2939", borderRadius: 999, color: "#fff", background: "#101828", boxShadow: "0 10px 26px rgba(0,0,0,.27)" };
const switcherButton: CSSProperties = { width: 32, height: 32, border: 0, borderRadius: "50%", color: "#101828", background: "#f8fafc", fontSize: 18, fontWeight: 800, cursor: "pointer" };
const switcherLabel: CSSProperties = { minWidth: 150, padding: "0 6px", textAlign: "center", fontSize: 12, fontWeight: 700 };
