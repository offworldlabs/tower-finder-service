import { useEffect, useMemo, useRef, useState } from "react";
import SearchForm from "./components/SearchForm";
import CopyLink from "./components/CopyLink";
import ResultsTable from "./components/ResultsTable";
import TowerMap from "./components/TowerMap";
import ThemeSwitch from "./components/ThemeSwitch";
import CellularAttribution from "./components/cellular/CellularAttribution";
import CellularBadge from "./components/cellular/CellularBadge";
import CellularMap from "./components/cellular/CellularMap";
import CellularNotice from "./components/cellular/CellularNotice";
import CellularResultsTable from "./components/cellular/CellularResultsTable";
import CellularSummary from "./components/cellular/CellularSummary";
import { distinct, isCellRow, type CellRow } from "./components/cellular/types";
import { useResolvedTheme } from "./context/ThemeContext";
import { CELLULAR_LIMIT, fetchTowers } from "./api";
import { formatAreaKm2 } from "./utils/format";
import { cellularFlagSet, useCellularMode } from "./utils/cellularMode";
import { readSharedSearch, writeSharedSearch } from "./utils/sharedSearch";
import type { CellularBlock, SearchRequest, Tower, TowerQuery } from "./types";

const NO_CELLS: CellRow[] = [];

const SOURCE_LABELS: Record<string, string> = {
  us: "United States (FCC)",
  ca: "Canada (ISED)",
  au: "Australia (ACMA)",
};

function SummaryStrip({
  towers,
  query,
  share,
}: {
  towers: Tower[];
  query: TowerQuery | null;
  share: boolean;
}) {
  if (!towers.length) return null;

  const bands = [...new Set(towers.map((t) => t.band))];
  const best = towers[0];

  return (
    <div className="summary-strip">
      <div className="card stat-card">
        <span className="stat-value">{towers.length}</span>
        <span className="label stat-label">Towers Found</span>
      </div>
      <div className="card stat-card">
        <span className="stat-value">{bands.join(", ")}</span>
        <span className="label stat-label">Bands</span>
      </div>
      {best && (
        <div className="card stat-card">
          <span className="stat-value">{best.callsign || "—"}</span>
          <span className="label stat-label">
            {/* The detect area is what the rank is sorted on, so the top pick
                says why it is top. Absent on an older backend: the label then
                reads exactly as it did before. */}
            Top Pick — {best.distance_km} km
            {best.expected_area_km2 != null &&
              ` · ${formatAreaKm2(best.expected_area_km2)} km²`}
          </span>
        </div>
      )}
      {query && (
        // Which country the server actually searched. Worth surfacing: when
        // "auto" is in play this is the only place the resolved region is
        // visible, and a wrong one is exactly the bug this UI used to cause.
        <div className="card stat-card">
          <span className="stat-value">{query.source.toUpperCase()}</span>
          <span className="label stat-label">{SOURCE_LABELS[query.source] || "Data Source"}</span>
        </div>
      )}
      {share && (
        <div className="card stat-card share-card">
          <CopyLink />
          <span className="label stat-label">Share this search</span>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [towers, setTowers] = useState<Tower[]>([]);
  const [query, setQuery] = useState<TowerQuery | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [highlighted, setHighlighted] = useState<Tower | null>(null);
  // True once any search has been submitted: the URL is a share link from then
  // on, whether or not the search succeeded.
  const [searched, setSearched] = useState(false);
  const theme = useResolvedTheme();
  // Read once. The form is seeded from it and the first search runs off the
  // same parsed values, so the two cannot disagree.
  const [shared] = useState(() => readSharedSearch(window.location.search));
  const autoRan = useRef(false);
  // The cellular version: asked for by the URL flag (read once, like the
  // share link, which keeps it), granted by the server's answer.
  const [cellularRequested] = useState(() => cellularFlagSet(window.location.search));
  const mode = useCellularMode(cellularRequested);
  // The `cellular` block of the last cellular answer, which says whether the
  // server could serve cell rows at all.
  const [cellularBlock, setCellularBlock] = useState<CellularBlock | null>(null);
  // A search asked for while the server's answer is still out: the latest only.
  const heldSearch = useRef<SearchRequest | null>(null);
  // Whether one is held, so the page can show it as under way. The ref above
  // is what the search runs from; this only drives the render.
  const [held, setHeld] = useState(false);

  async function runSearch(request: SearchRequest, cellularSearch: boolean) {
    const { lat, lon, altitude, source, frequencies } = request;
    // Before the request, not after it: a search that fails is still worth
    // sending to someone.
    writeSharedSearch(request);
    setSearched(true);
    setLoading(true);
    setError(null);
    setTowers([]);
    setQuery(null);
    setCellularBlock(null);

    try {
      const data = cellularSearch
        ? await fetchTowers(lat, lon, altitude, CELLULAR_LIMIT, source, frequencies, undefined, "cellular")
        : await fetchTowers(lat, lon, altitude, 20, source, frequencies);
      setTowers(data.towers);
      setQuery(data.query);
      setCellularBlock(data.cellular ?? null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  // Which search runs depends on the cellular answer, so a search asked for
  // before it (a shared link's own, or a submit) waits for it, and a later
  // one replaces it. Without the flag the mode is never "checking" and every
  // search runs at once, exactly as before.
  function handleSearch(request: SearchRequest) {
    if (mode.kind === "checking") {
      heldSearch.current = request;
      setHeld(true);
      return;
    }
    heldSearch.current = null;
    setHeld(false);
    runSearch(request, mode.kind === "on");
  }

  // Runs after the render that settled the mode, so the held search goes out
  // with the settled mode rather than the one it was asked under. Clearing
  // the hold and starting the search land in one render, so the loading card
  // stays up from the hold through the search.
  useEffect(() => {
    if (mode.kind === "checking" || !heldSearch.current) return;
    const request = heldSearch.current;
    heldSearch.current = null;
    setHeld(false);
    runSearch(request, mode.kind === "on");
  }, [mode]);

  // A complete link runs its search on load. Altitude is 0 unless the link
  // carried one, and the elevation lookup is not waited for: /api/towers
  // resolves ground elevation itself for 0, and the form fills the field for
  // display in parallel. The ref keeps StrictMode's double effect to one search.
  useEffect(() => {
    if (autoRan.current || !shared.request) return;
    autoRan.current = true;
    handleSearch(shared.request);
  }, []);

  const share = searched && !loading;
  // A held search reads as under way, the same as one in flight. Only the
  // URL flag can hold one, so without it this is `loading`.
  const searching = loading || held;

  // Everything below renders the page exactly as before unless the mode is
  // "on"; "checking" and "unavailable" are the normal page, bar the loading
  // card's wording while a search is held.
  const cellular = mode.kind === "on";
  // A cellular search the server could not serve: its detail stands in for
  // the results.
  const cellsUnavailable = cellular && cellularBlock !== null && cellularBlock.status !== "ok";
  // Cell rows only, whatever else the answer holds. The same array across a
  // hover's re-render, so the map keeps the view the operator left it at.
  const cells = useMemo(
    () => (cellular && !cellsUnavailable ? towers.filter(isCellRow) : NO_CELLS),
    [cellular, cellsUnavailable, towers],
  );
  const cellSources = useMemo(() => distinct(cells.map((t) => t.data_source)), [cells]);

  return (
    <div className="app">
      <header className="app-header">
        <span className="header-icon" aria-hidden="true">&#9041;</span>
        <h1>Tower Finder</h1>
        {cellular && <CellularBadge />}
        <span className="subtitle">Passive radar illuminator search</span>
        <div className="header-actions">
          <ThemeSwitch />
        </div>
      </header>

      <main className="app-body">
        {mode.kind === "unavailable" && (
          <CellularNotice title="Cellular view not available here">
            Showing the standard tower search.
          </CellularNotice>
        )}

        <div className="top-section">
          <SearchForm onSearch={handleSearch} loading={searching} initial={shared.initial} />
          {cellular ? (
            <CellularMap
              towers={cells}
              userLocation={query}
              highlighted={highlighted && isCellRow(highlighted) ? highlighted : null}
              theme={theme}
            />
          ) : (
            <TowerMap
              towers={towers}
              userLocation={query}
              highlighted={highlighted}
              theme={theme}
            />
          )}
        </div>

        {error && (
          <div className="error-banner">
            <span className="error-text">{error}</span>
            {share && <CopyLink />}
          </div>
        )}

        {searching && (
          <div className="card loading-section">
            <div className="spinner" />
            <div className="loading-bar">
              <div className="loading-bar-inner" />
            </div>
            <p className="loading-text">
              {/* Under the flag the wait is for cell sites until the server
                  says the cellular view is off. */}
              {cellular || mode.kind === "checking"
                ? "Querying cell site database — this may take up to a minute…"
                : "Querying broadcast licence database — this may take up to a minute…"}
            </p>
          </div>
        )}

        {cellular ? (
          <>
            {cellsUnavailable && (
              <CellularNotice title="Cell tower data unavailable">
                {cellularBlock.detail || "The server could not read its cell site data."}
                {share && <CopyLink />}
              </CellularNotice>
            )}

            <CellularSummary towers={cells} query={query} share={share} />

            {cells.length > 0 && <CellularResultsTable towers={cells} onHover={setHighlighted} />}

            {!loading && query && cells.length === 0 && !cellsUnavailable && (
              <div className="card no-results">
                <p>No cell towers found within {query.radius_km} km.</p>
                {share && <CopyLink />}
              </div>
            )}

            <CellularAttribution attribution={mode.status.attribution} activeSources={cellSources} />
          </>
        ) : (
          <>
            <SummaryStrip towers={towers} query={query} share={share} />

            {towers.length > 0 && <ResultsTable towers={towers} onHover={setHighlighted} />}

            {!loading && query && towers.length === 0 && (
              <div className="card no-results">
                <p>
                  No suitable broadcast towers found within {query.radius_km} km. Try a
                  location closer to a populated area, or widen the measured
                  frequencies.
                </p>
                {share && <CopyLink />}
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
