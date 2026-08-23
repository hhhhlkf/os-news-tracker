import type { CSSProperties } from "react";
import { buildCrawlFlow, type CrawlFlowStep } from "../discovery/crawlRecipeFlow";
import type { CrawlExecutionStep } from "../types";

function FlowSteps({ steps, nested = false }: { steps: CrawlFlowStep[]; nested?: boolean }) {
  return (
    <div style={nested ? nestedList : undefined}>
      {steps.map((step, index) => (
        <div key={`${step.title}-${index}`} style={{ position: "relative", paddingBottom: index === steps.length - 1 ? 0 : 14 }}>
          {index < steps.length - 1 && <div style={connector} />}
          <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
            <div style={iconBox}>{step.icon}</div>
            <div style={stepCard}>
              <div style={{ fontWeight: 750, fontSize: 13, color: "#101828" }}>{step.title}</div>
              {step.detail && <div style={detail}>{step.detail}</div>}
              {step.tags && step.tags.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 7 }}>
                  {step.tags.map((tag) => <span key={tag} style={tagStyle}>{tag}</span>)}
                </div>
              )}
              {step.children && step.children.length > 0 && <FlowSteps steps={step.children} nested />}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function CrawlRecipeFlow({ recipe, executionSteps }: { recipe: Record<string, unknown>; executionSteps?: CrawlExecutionStep[] }) {
  const steps = executionSteps?.length ? executionSteps : buildCrawlFlow(recipe);
  if (!steps.length) return <div style={empty}>此爬取方式还没有可展示的执行步骤。</div>;
  return (
    <section aria-label="爬取流程" style={panel}>
      <div style={{ fontSize: 14, fontWeight: 800, color: "#101828", marginBottom: 4 }}>爬取流程</div>
      <div style={{ fontSize: 12, color: "#667085", marginBottom: 14 }}>
        {recipe.recipe_type === "python_plugin"
          ? "根据已审核 crawler.py 静态解析生成；不执行代码，也不展示源码。"
          : "按顺序执行；循环步骤会重复其中的操作，直到满足终止条件。"}
      </div>
      <FlowSteps steps={steps} />
      <div style={output}>✓ 输出已去重的新闻条目</div>
    </section>
  );
}

const panel: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 10, background: "#fcfcfd", padding: 14, marginBottom: 16 };
const iconBox: CSSProperties = { position: "relative", zIndex: 1, width: 26, height: 26, borderRadius: 8, flexShrink: 0, display: "grid", placeItems: "center", background: "#eff8ff", border: "1px solid #b2ddff", color: "#175cd3", fontWeight: 800, fontSize: 15 };
const connector: CSSProperties = { position: "absolute", top: 27, left: 12, width: 2, height: "calc(100% - 22px)", background: "#b2ddff" };
const stepCard: CSSProperties = { minWidth: 0, flex: 1, padding: "4px 0 2px" };
const detail: CSSProperties = { color: "#475467", fontSize: 12, lineHeight: 1.5, overflowWrap: "anywhere", marginTop: 2 };
const tagStyle: CSSProperties = { borderRadius: 999, padding: "2px 7px", background: "#f2f4f7", color: "#475467", fontSize: 11, lineHeight: "16px" };
const nestedList: CSSProperties = { marginTop: 10, padding: "10px 10px 4px", borderLeft: "3px solid #84adff", borderRadius: 4, background: "#f5f8ff" };
const output: CSSProperties = { margin: "14px 0 0 36px", fontSize: 12, color: "#027a48", fontWeight: 700 };
const empty: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 14, color: "#667085", fontSize: 13, marginBottom: 16 };
