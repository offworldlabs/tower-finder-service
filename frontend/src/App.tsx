import { useState } from "react";
import SearchForm from "./components/SearchForm";
import ResultsTable from "./components/ResultsTable";
import TowerMap from "./components/TowerMap";
import { fetchTowers } from "./api";
import type { Tower, TowerQuery } from "./types";

const SOURCE_LABELS = {
  us: "United States (FCC)",
  ca: "Canada (ISED)",
  au: "Australia (ACMA)",
};

function SummaryStrip({ towers, query }: { towers: Tower[]; query: TowerQuery | null }) {
  if (!towers.length) return null;

  const ideal = towers.filter((t) => t.distance_class === "Ideal").length;
  const bands = [...new Set(towers.map((t) => t.band))];
  const best = towers[0];

  return (
    <div className="summary-strip">
      <div className="stat-card">
        <span className="stat-value">{towers.length}</span>
        <span className="stat-label">Towers Found</span>
      </div>
      <div className="stat-card">
        <span className="stat-value">{ideal}</span>
        <span className="stat-label">Ideal Range</span>
      </div>
      <div className="stat-card">
        <span className="stat-value">{bands.join(", ")}</span>
        <span className="stat-label">Bands</span>
      </div>
      {best && (
        <div className="stat-card">
          <span className="stat-value">{best.callsign || "—"}</span>
          <span className="stat-label">Top Pick — {best.distance_km} km</span>
        </div>
      )}
      {query && (
        // Which country the server actually searched. Worth surfacing: when
        // "auto" is in play this is the only place the resolved region is
        // visible, and a wrong one is exactly the bug this UI used to cause.
        <div className="stat-card">
          <span className="stat-value">{query.source.toUpperCase()}</span>
          <span className="stat-label">{SOURCE_LABELS[query.source] || "Data Source"}</span>
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

  async function handleSearch({ lat, lon, altitude, source }) {
    setLoading(true);
    setError(null);
    setTowers([]);
    setQuery(null);

    try {
      const data = await fetchTowers(lat, lon, altitude, 20, source);
      setTowers(data.towers);
      setQuery(data.query);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <span className="header-icon">&#9041;</span>
        <h1>Tower Finder</h1>
        <span className="subtitle">Passive Radar Illuminator Search</span>
      </header>

      <main className="app-body">
        <div className="top-section">
          <SearchForm onSearch={handleSearch} loading={loading} />
          <TowerMap towers={towers} userLocation={query} highlighted={highlighted} />
        </div>

        {error && <div className="error-banner">{error}</div>}

        {loading && (
          <div className="loading-section">
            <div className="spinner" />
            <div className="loading-bar">
              <div className="loading-bar-inner" />
            </div>
            <p className="loading-text">
              Querying broadcast licence database — this may take up to a minute…
            </p>
          </div>
        )}

        <SummaryStrip towers={towers} query={query} />

        {towers.length > 0 && <ResultsTable towers={towers} onHover={setHighlighted} />}

        {!loading && query && towers.length === 0 && (
          <p className="no-results">
            No suitable broadcast towers found within {query.radius_km} km.
          </p>
        )}
      </main>
    </div>
  );
}
