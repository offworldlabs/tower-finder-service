import type { ReactNode } from "react";
import "./CellularFrame.css";

interface Props {
  title: string;
  children?: ReactNode;
  tone?: "info" | "warning";
}

export default function CellularNotice({ title, children, tone = "warning" }: Props) {
  return (
    <div className={`card cell-notice cell-notice--${tone}`} role="status">
      <svg
        className="cell-notice-icon"
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        {tone === "info" ? (
          <>
            <path d="M12 11v5" />
            <path d="M12 8h.01" />
          </>
        ) : (
          <>
            <path d="M12 7v6" />
            <path d="M12 16.5h.01" />
          </>
        )}
      </svg>
      <div className="cell-notice-body">
        <strong className="cell-notice-title">{title}</strong>
        {children && <span className="cell-notice-text">{children}</span>}
      </div>
    </div>
  );
}
