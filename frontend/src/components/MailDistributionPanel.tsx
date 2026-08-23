import { useState } from "react";
import type { ItemQueryParams } from "../api/client";
import type { MailTemplate } from "../mail/types";
import { MailImmediateSendPanel } from "./MailImmediateSendPanel";
import { MailTrendDistributionPanel } from "./MailTrendDistributionPanel";

type DistributionKind = "news" | "trend";

const choices: Array<{
  key: DistributionKind;
  title: string;
  description: string;
}> = [
  { key: "news", title: "当前筛选快照", description: "发送当前筛选出的新闻" },
  { key: "trend", title: "趋势来源", description: "按身份模板方向发送趋势与热点" },
];

export function MailDistributionPanel(props: {
  homeFilters: ItemQueryParams;
  onTemplateSaved: (template: MailTemplate, kind: DistributionKind) => void;
}) {
  const { homeFilters, onTemplateSaved } = props;
  const [kind, setKind] = useState<DistributionKind>("news");

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, padding: 4, border: "1px solid #eaecf0", borderRadius: 12, background: "#f8fafc" }}>
        {choices.map((choice) => {
          const active = choice.key === kind;
          return (
            <button
              key={choice.key}
              type="button"
              onClick={() => setKind(choice.key)}
              style={{
                flex: 1,
                border: active ? "1px solid #bfd7ff" : "1px solid transparent",
                borderRadius: 9,
                padding: "8px 10px",
                textAlign: "left",
                background: active ? "#fff" : "transparent",
                boxShadow: active ? "0 2px 5px rgba(16,24,40,0.06)" : "none",
                cursor: "pointer",
              }}
            >
              <strong style={{ display: "block", fontSize: 13, color: active ? "#175cd3" : "#344054", marginBottom: 2 }}>{choice.title}</strong>
              <span style={{ display: "block", fontSize: 11, color: "#667085", lineHeight: 1.45 }}>{choice.description}</span>
            </button>
          );
        })}
      </div>

      {kind === "news" ? (
        <MailImmediateSendPanel homeFilters={homeFilters} onTemplateSaved={(template) => onTemplateSaved(template, kind)} />
      ) : (
        <MailTrendDistributionPanel onTemplateSaved={(template) => onTemplateSaved(template, kind)} />
      )}
    </div>
  );
}
