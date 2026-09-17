import React, { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api.js";
import { useSiteNames } from "../hooks/useSiteNames.js";
import SiteSwitcher from "../components/SiteSwitcher.jsx";
import SightingCard from "../components/SightingCard.jsx";
import "./Sightings.css";

const PAGE_SIZE = 40;
const EMPTY_FILTERS = { camera: "", species: "", day: "" };

export default function Sightings() {
  const sites = useSiteNames();
  const [site, setSite] = useState(null);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [result, setResult] = useState(null);
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    if (sites && sites.length > 0 && !site) setSite(sites[0]);
  }, [sites, site]);

  const load = useCallback(async (offset) => {
    if (!site) return;
    const params = { site, limit: PAGE_SIZE, offset };
    if (filters.camera) params.camera = filters.camera;
    if (filters.species) params.species = filters.species;
    if (filters.day) params.day = filters.day;
    try {
      const data = await api.sightings(params);
      setResult(data);
      setItems((prev) => (offset === 0 ? data.sightings : [...prev, ...data.sightings]));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "network-error");
    } finally {
      setLoadingMore(false);
    }
  }, [site, filters]);

  useEffect(() => {
    setResult(null);
    setItems([]);
    load(0);
  }, [load]);

  function updateFilter(key, value) {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }

  const filtersActive = Object.values(filters).some(Boolean);

  if (sites === null) return <div className="state-page skeleton-block" style={{ height: 200 }} />;
  if (sites.length === 0) {
    return (
      <div className="state-page">
        <h2>No cameras configured</h2>
        <p>Sightings need at least one configured site.</p>
      </div>
    );
  }

  return (
    <div className="sightings-page">
      <div className="sightings-head">
        <span className="tick-label">Sightings</span>
        <SiteSwitcher sites={sites} value={site} onChange={setSite} />
      </div>

      {result && (
        <div className="sightings-filters">
          <FilterSelect
            label="Camera" value={filters.camera} options={result.cameras}
            onChange={(v) => updateFilter("camera", v)}
          />
          <FilterSelect
            label="Species" value={filters.species} options={result.species}
            onChange={(v) => updateFilter("species", v)}
          />
          <FilterSelect
            label="Day" value={filters.day} options={result.days}
            onChange={(v) => updateFilter("day", v)}
          />
          {filtersActive && (
            <button type="button" className="sightings-clear" onClick={() => setFilters(EMPTY_FILTERS)}>
              Clear filters
            </button>
          )}
        </div>
      )}

      {error && (
        <div className="state-page">
          <h2>Can't load sightings</h2>
          <p>{error}</p>
        </div>
      )}

      {!error && result === null && (
        <div className="sightings-grid">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="sighting-card-skeleton skeleton-block" aria-hidden="true" />
          ))}
        </div>
      )}

      {!error && result && result.total === 0 && !filtersActive && (
        <div className="state-page">
          <h2>No confirmed sightings yet</h2>
          <p>Sightings appear here once an operator confirms a detection as a real bird in Review.</p>
        </div>
      )}

      {!error && result && result.total === 0 && filtersActive && (
        <div className="state-page">
          <h2>No sightings match these filters</h2>
          <button type="button" className="sightings-clear" onClick={() => setFilters(EMPTY_FILTERS)}>
            Clear filters
          </button>
        </div>
      )}

      {!error && result && result.total > 0 && (
        <>
          <div className="sightings-grid">
            {items.map((s) => <SightingCard key={s.id} site={site} sighting={s} />)}
          </div>
          {items.length < result.total && (
            <button
              type="button"
              className="sightings-load-more"
              disabled={loadingMore}
              onClick={() => { setLoadingMore(true); load(items.length); }}
            >
              {loadingMore ? "Loading…" : `Load more (${result.total - items.length} more)`}
            </button>
          )}
        </>
      )}
    </div>
  );
}

function FilterSelect({ label, value, options, onChange }) {
  if (!options || options.length === 0) return null;
  return (
    <label className="sightings-filter">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {options.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
      </select>
    </label>
  );
}
