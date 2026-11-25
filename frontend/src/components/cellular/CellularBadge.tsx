import "./CellularFrame.css";

export default function CellularBadge() {
  return (
    <span className="cell-mode-badge" aria-label="Cellular view">
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M12 12v9" />
        <path d="M9 21h6" />
        <circle cx="12" cy="10" r="1.5" fill="currentColor" />
        <path d="M8.5 6.5a5 5 0 0 0 0 7" />
        <path d="M15.5 6.5a5 5 0 0 1 0 7" />
        <path d="M5.5 3.5a9 9 0 0 0 0 13" />
        <path d="M18.5 3.5a9 9 0 0 1 0 13" />
      </svg>
      Cellular
    </span>
  );
}
