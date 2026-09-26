import { useEffect, useRef, useState } from "react";
import SearchForm from "./components/SearchForm";
import CopyLink from "./components/CopyLink";
import ResultsTable from "./components/ResultsTable";
import TowerMap from "./components/TowerMap";
import ThemeSwitch from "./components/ThemeSwitch";
import { useResolvedTheme } from "./context/ThemeContext";
import { fetchTowers } from "./api";
import { formatAreaKm2 } from "./utils/format";
import { readSharedSearch, writeSharedSearch } from "./utils/sharedSearch";
import type { SearchRequest, Tower, TowerQuery } from "./types";

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

  async function handleSearch(request: SearchRequest) {
    const { lat, lon, altitude, source, frequencies } = request;
    // Before the request, not after it: a search that fails is still worth
    // sending to someone.
    writeSharedSearch(request);
    setSearched(true);
    setLoading(true);
    setError(null);
    setTowers([]);
    setQuery(null);

    try {
      const data = await fetchTowers(lat, lon, altitude, 20, source, frequencies);
      setTowers(data.towers);
      setQuery(data.query);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

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

  return (
    <div className="app">
      <header className="app-header">
        <span className="header-icon" aria-hidden="true">&#9041;</span>
        <h1>Tower Finder</h1>
        <span className="subtitle">Passive radar illuminator search</span>
        <div className="header-actions">
          <ThemeSwitch />
        </div>
      </header>

      <main className="app-body">
        <div className="top-section">
          <SearchForm onSearch={handleSearch} loading={loading} initial={shared.initial} />
          <TowerMap
            towers={towers}
            userLocation={query}
            highlighted={highlighted}
            theme={theme}
          />
        </div>

        {error && (
          <div className="error-banner">
            <span className="error-text">{error}</span>
            {share && <CopyLink />}
          </div>
        )}

        {loading && (
          <div className="card loading-section">
            <div className="spinner" />
            <div className="loading-bar">
              <div className="loading-bar-inner" />
            </div>
            <p className="loading-text">
              Querying broadcast licence database — this may take up to a minute…
            </p>
          </div>
        )}

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
      </main>
    </div>
  );
}
