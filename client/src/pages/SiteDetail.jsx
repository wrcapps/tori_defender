import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useCameras } from "../hooks/useCameras.js";
import { useAuth } from "../AuthContext.jsx";
import { api } from "../api.js";
import { groupByMast } from "../lib/grouping.js";
import MastMap from "../components/MastMap.jsx";
import CameraCard from "../components/CameraCard.jsx";
import CameraWall from "../components/CameraWall.jsx";
import "./SiteDetail.css";

export default function SiteDetail() {
  const { site } = useParams();
  const { role } = useAuth();
  const { rows, error, reconnecting } = useCameras();
  const [activeMast, setActiveMast] = useState(null);
  const [busyCamera, setBusyCamera] = useState(null);
  // Per-tile quick looks only -- "watch everything at once" is its own page
  // (Matrix), not a state this page fakes by turning on every tile's preview
  // in place. See MILESTONES.md for why that was the wrong shape the first
  // time around: a real "wall of cameras" view is a destination someone
  // navigates to on purpose, not a toggle bolted onto the mast grid.
  const [previewing, setPreviewing] = useState(() => new Set());
  const [capacity, setCapacity] = useState(null);
  const [pollTick, setPollTick] = useState(0);
  // "grid" (security-wall) is the default live view; "layout" is today's
  // mast-schematic view, kept one click away rather than removed.
  const [view, setView] = useState("grid");
  const [recentByCamera, setRecentByCamera] = useState({});

  useEffect(() => { setPollTick((t) => t + 1); }, [rows]);

  useEffect(() => {
    let cancelled = false;
    api.get(`/api/capacity?${new URLSearchParams({ site })}`)
      .then((data) => !cancelled && setCapacity(data))
      .catch(() => {});
    return () => { cancelled = true; };
  }, [site]);

  // Feeds the grid's per-tile activity chip -- its own page-scoped poll
  // (same pattern as the capacity fetch above), not the shell-wide
  // notification feed, since this only needs "latest per camera on this
  // site", not the cross-site event queue.
  useEffect(() => {
    let cancelled = false;
    function load() {
      api.recentDetections()
        .then((detections) => {
          if (cancelled) return;
          const bySite = detections.filter((r) => r.site === site);
          const byCamera = {};
          for (const row of bySite) if (!byCamera[row.camera]) byCamera[row.camera] = row;
          setRecentByCamera(byCamera);
        })
        .catch(() => {});
    }
    load();
    const id = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(id); };
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

  const masts = groupByMast(cameras);

  function togglePreview(name) {
    setPreviewing((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }

  async function toggleRecording(name, enabled) {
    setBusyCamera(name);
    try {
      await api.post("/api/recording", { name, enabled });
    } finally {
      setBusyCamera(null);
    }
  }

  const overBudget = capacity && capacity.peak_demand > capacity.gpu_fps;

  return (
    <div className="site-detail">
      {reconnecting && (
        <div className="live-banner" role="status">
          Reconnecting to the server — the last known status is shown below.
        </div>
      )}
      <div className="site-detail-head">
        <Link to="/app/live" className="site-detail-back">← Sites</Link>
        <h2 className="site-detail-title">{site}</h2>

        <div className="site-detail-watchall">
          {/* Operators see the exact numeric budget they need to size camera
              load; a client gets a plain-language line, and only once it
              actually matters (over budget) -- raw "peak_demand / gpu_fps"
              numbers are infra jargon with no action a client can take. */}
          {capacity && role === "operator" && (
            <span className={`capacity-note ${overBudget ? "is-over" : ""}`}>
              {capacity.cameras} camera{capacity.cameras === 1 ? "" : "s"} · {capacity.peak_demand.toFixed(1)}
              {" / "}{capacity.gpu_fps.toFixed(1)} GPU budget if all watched at once
            </span>
          )}
          {capacity && role !== "operator" && overBudget && (
            <span className="capacity-note is-over">
              Watching every camera at once may slow things down here
            </span>
          )}
          <div className="view-toggle" role="group" aria-label="Live view">
            <button
              type="button" className={view === "grid" ? "is-active" : ""}
              onClick={() => setView("grid")}
            >
              Grid
            </button>
            <button
              type="button" className={view === "layout" ? "is-active" : ""}
              onClick={() => setView("layout")}
            >
              Layout
            </button>
          </div>
          <Link to={`/app/live/${encodeURIComponent(site)}/matrix`} className="watch-all-btn">
            Watch all (360°)
          </Link>
        </div>
      </div>

      {/* Shown here too, before anyone even clicks through to Matrix -- the
          consequence (real bandwidth/GPU load) should be visible at the
          point of decision, not just on the page it leads to. */}
      {overBudget && role === "operator" && (
        <div className="live-banner" role="status">
          {capacity.warning}
        </div>
      )}

      {view === "grid" ? (
        <CameraWall cameras={cameras} recentByCamera={recentByCamera} pollTick={pollTick} />
      ) : (
      <div className="site-detail-layout">
        <div className="site-detail-map">
          <span className="tick-label">Layout</span>
          <MastMap
            groups={masts}
            activeKey={activeMast}
            onSelect={(key) => setActiveMast(key === activeMast ? null : key)}
          />
        </div>

        <div className="site-detail-cameras">
          <span className="tick-label">Cameras</span>
          {masts.map((mast, i) => (
            <div
              key={mast.key}
              className={`mast-group ${activeMast && activeMast !== mast.key ? "is-dimmed" : ""}`}
            >
              {masts.length > 1 && <div className="mast-group-label">Mast {i + 1}</div>}
              <div className="mast-group-grid">
                {mast.cameras.map((cam) => (
                  <Link
                    key={cam.name}
                    to={`/app/live/${encodeURIComponent(site)}/camera/${encodeURIComponent(cam.name)}`}
                    className="mast-camera-link"
                  >
                    <CameraCard
                      camera={cam}
                      canRecord={role === "operator"}
                      showDiagnostics={role === "operator"}
                      recordingBusy={busyCamera === cam.name}
                      onToggleRecording={toggleRecording}
                      previewing={previewing.has(cam.name)}
                      onTogglePreview={togglePreview}
                      pollTick={pollTick}
                    />
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
      )}
    </div>
  );
}
