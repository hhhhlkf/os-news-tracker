import { useState, type CSSProperties, type MouseEvent } from "react";

type Props = {
  onClick: (event: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  title: string;
  "aria-label"?: string;
};

/** Compact borderless trash icon — avoids emoji glyphs that render with a red badge. */
export function DeleteIconButton({ onClick, disabled = false, title, "aria-label": ariaLabel }: Props) {
  const [hover, setHover] = useState(false);

  const style: CSSProperties = {
    border: "none",
    background: hover && !disabled ? "#fef3f2" : "transparent",
    color: hover && !disabled ? "#b42318" : "#98a2b3",
    width: 28,
    height: 28,
    padding: 0,
    borderRadius: 6,
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    cursor: disabled ? "wait" : "pointer",
    opacity: disabled ? 0.55 : 1,
    lineHeight: 0,
  };

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel ?? title}
      style={style}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m2 0v12a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V7h10ZM10 11v6M14 11v6"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}
