import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useCameras } from "../hooks/useCameras.js";
import { api } from "../api.js";
import MatrixTile from "../components/MatrixTile.jsx";
import "./Matrix.css";

// The dedicated "watch everything at once" page -- reached only by an
// explicit click (SiteDetail's "Watch all" link), never opened as a side
// effect of visiting a site. Every tile here streams live the moment this
// page mounts and stops the moment it unmounts (leaving, closing the tab),
// same viewer-counted lifecycle every other live view uses -- this page
// doesn't add a new one, it just puts many of them on screen together.
export default function Matrix() {
  const { site } = useParams();
  const { rows, error, reconnecting } = useCameras();
  const [capacity, setCapacity] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api.get(`/api/capacity?${new URLSearchParams({ site })}`)
      .then((data) => !cancelled && setCapacity(data))
      .catch(() => {});
    return () => { cancelled = true; };
  }, [site]);

  if (rows === null && !error) return <div className="state-page skeleton-block" style={{ height: 320 }} />;
  if (error && rows === null) {
    return (
      <div className="state-page">
        <h2>Can't load this site</h2>
        <p>{error}</p>
      </div>
    );
  }

  const cameras = (rows || []).filter((c) => c.site === site);
  if (cameras.length === 0) {
    return (
      <div className="state-page">
        <h2>Unknown site</h2>
        <p>"{site}" isn't in the current configuration.</p>
        <Link to="/app/live">Back to sites</Link>
      </div>
    );
  }

  const overBudget = capacity && capacity.peak_demand > capacity.gpu_fps;

  return (
    <div className="matrix-page">
      {reconnecting && (
        <div className="live-banner" role="status">
          Reconnecting to the server — tiles show the last known status.
        </div>
      )}
      <div className="matrix-head">
        <Link to={`/app/live/${encodeURIComponent(site)}`} className="matrix-back">← {site}</Link>
        <h2 className="matrix-title">{site} · all cameras</h2>
        {capacity && (
          <span className={`capacity-note ${overBudget ? "is-over" : ""}`}>
            {capacity.cameras} cameras · {capacity.peak_demand.toFixed(1)} / {capacity.gpu_fps.toFixed(1)} GPU budget
          </span>
        )}
      </div>

      {overBudget && (
        <div className="live-banner" role="status">
          {capacity.warning}
        </div>
      )}

      <div className="matrix-grid">
        {cameras.map((cam) => <MatrixTile key={cam.name} camera={cam} />)}
      </div>
    </div>
  );
}
