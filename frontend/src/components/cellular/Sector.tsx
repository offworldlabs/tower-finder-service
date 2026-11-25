import "./cellular.css";

/** Degrees to at most one decimal, so 12.2 stays 12.2 and 285.0 reads 285. */
export function formatDegrees(deg: number): string {
  return `${Number(deg.toFixed(1))}°`;
}

/** A small arrow pointing along a true-north bearing. Shaft and open head
 *  rather than a solid dart: at 12 px a dart's wing reads as a second tip. */
export function SectorArrow({ deg }: { deg: number }) {
  return (
    <svg
      className="cell-sector-arrow"
      viewBox="0 0 12 12"
      width="12"
      height="12"
      aria-hidden="true"
      style={{ transform: `rotate(${deg}deg)` }}
    >
      <path d="M6 10.8V1.8M2.4 5.4L6 1.8L9.6 5.4" />
    </svg>
  );
}
