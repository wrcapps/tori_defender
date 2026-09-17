import React, { useEffect, useState } from "react";
import { api, ApiError } from "../api.js";
import { useSiteNames } from "../hooks/useSiteNames.js";
import SiteSwitcher from "../components/SiteSwitcher.jsx";
import "./Species.css";

export default function Species() {
  const sites = useSiteNames();
  const [site, setSite] = useState(null);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (sites && sites.length > 0 && !site) setSite(sites[0]);
  }, [sites, site]);

  useEffect(() => {
    if (!site) return;
    setSummary(null);
    api.speciesSummary(site)
      .then(setSummary)
      .catch((err) => setError(err instanceof ApiError ? err.message : "network-error"));
  }, [site]);

  if (sites === null) return <div className="state-page skeleton-block" style={{ height: 200 }} />;
  if (sites.length === 0) {
    return (
      <div className="state-page">
        <h2>No cameras configured</h2>
        <p>Species need at least one configured site.</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="state-page">
        <h2>Can't load species</h2>
        <p>{error}</p>
      </div>
    );
  }

  return (
    <div className="species-page">
      <div className="species-head">
        <span className="tick-label">Species</span>
        <SiteSwitcher sites={sites} value={site} onChange={setSite} />
      </div>

      {summary === null && <div className="state-page skeleton-block" style={{ height: 300 }} />}

      {summary && summary.total_sightings === 0 && (
        <div className="state-page">
          <h2>No confirmed sightings yet</h2>
          <p>Species stats build up from confirmed sightings — nothing has been confirmed on this site yet.</p>
        </div>
      )}

      {summary && summary.total_sightings > 0 && (
        <>
          <div className="species-stats">
            <StatCard label="Species identified" value={summary.total_species} />
            <StatCard label="Confirmed sightings" value={summary.total_sightings} />
            <StatCard label="This month" value={summary.sightings_this_month} />
            <StatCard
              label="Most active camera"
              value={summary.most_active_camera ? summary.most_active_camera.camera : "—"}
              sub={summary.most_active_camera ? `${summary.most_active_camera.count} sightings` : null}
            />
          </div>

          {summary.total_species === 0 ? (
            <div className="state-page species-untagged">
              <h2>No species tagged yet</h2>
              <p>
                {summary.untagged_count} confirmed sighting{summary.untagged_count === 1 ? "" : "s"} on
                this site {summary.untagged_count === 1 ? "hasn't" : "haven't"} been identified by species
                yet — that happens during Review, as an optional field next to each verdict.
              </p>
            </div>
          ) : (
            <div className="species-grid">
              {summary.species.map((s) => <SpeciesCard key={s.name} site={site} species={s} />)}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function StatCard({ label, value, sub }) {
  return (
    <div className="species-stat-card">
      <span className="species-stat-value">{value}</span>
      <span className="species-stat-label">{label}</span>
      {sub && <span className="species-stat-sub">{sub}</span>}
    </div>
  );
}

function SpeciesCard({ site, species }) {
  const [imgFailed, setImgFailed] = useState(false);
  const hasImage = species.best_crop && !imgFailed;
  return (
    <div className="species-card">
      <div className="species-card-media">
        {hasImage ? (
          <img
            src={`/img/${encodeURIComponent(site)}/${species.best_crop}`}
            alt={species.name}
            onError={() => setImgFailed(true)}
          />
        ) : (
          <div className="species-card-fallback" aria-hidden="true">🕊️</div>
        )}
      </div>
      <div className="species-card-body">
        <span className="species-card-name">{species.name}</span>
        <span className="species-card-count">{species.count} sighting{species.count === 1 ? "" : "s"}</span>
        <span className="species-card-seen">last seen {species.last_seen}</span>
      </div>
    </div>
  );
}
