import { useState, useEffect, useRef } from "react";
import { fetchElevation } from "../api";
import "./SearchForm.css";

// Both mirror parse_user_frequencies in services/tower_ranking.py, which keeps
// at most ten values and only those in 0 < v < 10000. Anything else is dropped
// there without a word, so the form declines to offer it in the first place.
// The server's upper bound is exclusive and the input's `max` is inclusive, so
// exactly 10000 passes the browser and is dropped below; no illuminator sits
// there.
const MAX_FREQUENCIES = 10;
const MAX_FREQUENCY_MHZ = 10000;

/**
 * Location entry for a tower search.
 *
 * Note what this component deliberately does NOT do: guess the country from
 * the coordinates. It used to, with lat/lon bounding boxes that checked Canada
 * before the US along a flat 42°N line, so every US point above that latitude
 * — New England, Michigan, Minnesota, the Pacific Northwest — was pinned to
 * "ca" and searched against Canadian ISED data. Worse, it wrote that guess into
 * the request, so the server's polygon lookup never got to correct it. The
 * default is now "auto" and the server decides.
 */
export default function SearchForm({ onSearch, loading }) {
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [altitude, setAltitude] = useState("");
  const [source, setSource] = useState("auto");
  const [frequencies, setFrequencies] = useState([""]);
  const [showFrequencies, setShowFrequencies] = useState(false);
  const [geoError, setGeoError] = useState(null);
  const [geoLoading, setGeoLoading] = useState(false);
  const altitudeManual = useRef(false);

  // One reading of the entered rows, so what is submitted and what the collapsed
  // toggle counts can never disagree.
  const validFrequencies = frequencies
    .map((f) => parseFloat(f))
    .filter((f) => !isNaN(f) && f > 0 && f < MAX_FREQUENCY_MHZ);

  // Auto-lookup elevation when lat/lon change and altitude hasn't been set by
  // hand. Debounced and aborted so typing a coordinate doesn't fire one
  // un-cancellable request per keystroke.
  useEffect(() => {
    if (altitudeManual.current) return;
    const parsedLat = parseFloat(lat);
    const parsedLon = parseFloat(lon);
    if (isNaN(parsedLat) || isNaN(parsedLon)) return;

    const controller = new AbortController();
    const timer = setTimeout(() => {
      fetchElevation(parsedLat, parsedLon, controller.signal)
        .then((elev) => {
          if (!controller.signal.aborted && elev != null && !altitudeManual.current) {
            setAltitude(Math.round(elev).toString());
          }
        })
        .catch(() => {});
    }, 400);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [lat, lon]);

  function handleSubmit(e) {
    e.preventDefault();
    const parsedLat = parseFloat(lat);
    const parsedLon = parseFloat(lon);
    if (isNaN(parsedLat) || isNaN(parsedLon)) return;
    onSearch({
      lat: parsedLat,
      lon: parsedLon,
      altitude: parseFloat(altitude) || 0,
      source,
      frequencies: validFrequencies,
    });
  }

  function useMyLocation() {
    if (!navigator.geolocation) {
      setGeoError("Geolocation not supported by your browser");
      return;
    }
    setGeoError(null);
    setGeoLoading(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setGeoLoading(false);
        setLat(pos.coords.latitude.toFixed(6));
        setLon(pos.coords.longitude.toFixed(6));
        if (pos.coords.altitude != null) {
          setAltitude(Math.round(pos.coords.altitude).toString());
        }
      },
      (err) => {
        setGeoLoading(false);
        const msgs = {
          1: "Location access denied — please allow location in browser settings",
          2: "Location unavailable",
          3: "Location request timed out",
        };
        setGeoError(msgs[err.code] || err.message);
      },
      { timeout: 10000, maximumAge: 60000, enableHighAccuracy: false }
    );
  }

  return (
    <form className="search-form" onSubmit={handleSubmit}>
      <h2>Location</h2>

      <div className="field-row">
        <label>
          Latitude
          <input
            type="number"
            step="any"
            min={-90}
            max={90}
            value={lat}
            onChange={(e) => setLat(e.target.value)}
            placeholder="e.g. 38.8977"
            required
          />
        </label>
        <label>
          Longitude
          <input
            type="number"
            step="any"
            min={-180}
            max={180}
            value={lon}
            onChange={(e) => setLon(e.target.value)}
            placeholder="e.g. -77.0365"
            required
          />
        </label>
      </div>

      <div className="field-row">
        <label>
          Altitude (m)
          <input
            type="number"
            step="any"
            min={0}
            value={altitude}
            onChange={(e) => {
              setAltitude(e.target.value);
              altitudeManual.current = e.target.value !== "";
            }}
            placeholder="Auto-detected"
          />
        </label>
        <label>
          Data Source
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="auto">Auto-detect from coordinates</option>
            <option value="us">United States (FCC)</option>
            <option value="ca">Canada (ISED)</option>
            <option value="au">Australia (ACMA)</option>
          </select>
        </label>
      </div>

      <div className="freq-toggle">
        <button
          type="button"
          className="btn-link"
          onClick={() => setShowFrequencies(!showFrequencies)}
        >
          {showFrequencies
            ? "Hide Measured Frequencies"
            : validFrequencies.length > 0
              ? `Measured Frequencies (${validFrequencies.length})`
              : "Add Measured Frequencies"}
        </button>
      </div>

      {showFrequencies && (
        <div className="freq-section">
          <span className="freq-label">Measured Frequencies (MHz)</span>
          <div className="freq-inputs">
            {frequencies.map((freq, i) => (
              <div key={i} className="freq-row">
                <input
                  type="number"
                  step="any"
                  min={0}
                  max={MAX_FREQUENCY_MHZ}
                  value={freq}
                  aria-label={`Frequency ${i + 1} (MHz)`}
                  onChange={(e) => {
                    const updated = [...frequencies];
                    updated[i] = e.target.value;
                    setFrequencies(updated);
                  }}
                  placeholder={`Freq ${i + 1} (MHz)`}
                />
                {frequencies.length > 1 && (
                  <button
                    type="button"
                    className="btn-remove-freq"
                    aria-label={`Remove frequency ${i + 1}`}
                    onClick={() => setFrequencies(frequencies.filter((_, j) => j !== i))}
                  >
                    &times;
                  </button>
                )}
              </div>
            ))}
          </div>
          {frequencies.length < MAX_FREQUENCIES && (
            <button
              type="button"
              className="btn-add-freq"
              onClick={() => setFrequencies([...frequencies, ""])}
            >
              + Add Frequency
            </button>
          )}
        </div>
      )}

      <div className="form-actions">
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? "Searching…" : "Find Towers"}
        </button>
        <button
          type="button"
          className="btn-secondary"
          onClick={useMyLocation}
          disabled={loading || geoLoading}
        >
          {geoLoading ? "Getting location…" : "Use My Location"}
        </button>
      </div>

      {geoError && <p className="geo-error">{geoError}</p>}
    </form>
  );
}
