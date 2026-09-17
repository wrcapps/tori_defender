import React from "react";
import { Link } from "react-router-dom";
import { useCameras } from "../hooks/useCameras.js";
import { groupBySite, groupByMast, summarize } from "../lib/grouping.js";
import StatusPill from "../components/StatusPill.jsx";
import "./SitesList.css";

export default function SitesList() {
  const { rows, error, reconnecting } = useCameras();

  if (rows === null && !error) return <SitesSkeleton />;

  if (error && rows === null) {
    return (
      <div className="state-page">
        <h2>Can't load sites</h2>
        <p>{error}</p>
      </div>
    );
  }

  const bySite = groupBySite(rows || []);
  const siteNames = Object.keys(bySite);

  if (siteNames.length === 0) {
    return (
      <div className="state-page">
        <h2>No cameras configured</h2>
        <p>Nothing is set up to show here yet.</p>
      </div>
    );
  }

  return (
    <div className="sites-page">
      {reconnecting && (
        <div className="live-banner" role="status">
          Reconnecting to the server — the last known status is shown below.
        </div>
      )}
      <span className="tick-label">Sites</span>
      <div className="sites-grid">
        {siteNames.map((site) => {
          const cameras = bySite[site];
          const masts = groupByMast(cameras);
          const health = summarize(cameras);
          return (
            <Link key={site} to={`/app/live/${encodeURIComponent(site)}`} className="site-card hover-lift">
              <div className="site-card-head">
                <span className="site-card-name">{site}</span>
                <StatusPill kind={health.kind} />
              </div>
              <div className="site-card-stats">
                <Stat label="Cameras" value={cameras.length} />
                <Stat label="Masts" value={masts.length} />
                <Stat label="Live" value={cameras.filter((c) => c.live && c.stable).length} />
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div className="site-card-stat">
      <span className="site-card-stat-value">{value}</span>
      <span className="site-card-stat-label">{label}</span>
    </div>
  );
}

function SitesSkeleton() {
  return (
    <div className="sites-page">
      <div className="sites-grid">
        {Array.from({ length: 2 }).map((_, i) => (
          <div key={i} className="site-card-skeleton skeleton-block" aria-hidden="true" />
        ))}
      </div>
    </div>
  );
}
