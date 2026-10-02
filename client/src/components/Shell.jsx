import React, { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { useAuth } from "../AuthContext.jsx";
import { useDetectionEvents } from "../hooks/useDetectionEvents.js";
import NotificationBell from "./NotificationBell.jsx";
import AcquisitionButton from "./AcquisitionButton.jsx";
import { useAcquisition } from "../hooks/useAcquisition.js";
import { useInbox } from "../hooks/useInbox.js";
import "./Shell.css";

const CLIENT_NAV = [
  { to: "/app/live", label: "Live" },
  { to: "/app/sightings", label: "Sightings" },
  { to: "/app/species", label: "Species" },
];

// Operator-only SPA routes -- migrated off the legacy static /review and
// /dataset pages (M4b): same login, same backend API, now inside this one
// app's shell and nav instead of a separate full-page-reload tool.
const OPERATOR_LINKS = [
  { to: "/app/inbox", label: "Inbox", badge: true },
  { to: "/app/review", label: "Review" },
  { to: "/app/dataset", label: "Dataset" },
  { to: "/app/settings", label: "Settings" },
];

export default function Shell() {
  const { username, role, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  // One feed for the whole shell, not per-page -- a detection notification
  // should still reach whoever's watching Review or Sightings, not just
  // someone sitting on the Live page.
  const { events, dismiss, clearAll } = useDetectionEvents();
  const acquisition = useAcquisition();
  const inbox = useInbox(role === "operator");
  // First run: send the operator to the setup once, from wherever they landed.
  const navigate = useNavigate();
  const location = useLocation();
  useEffect(() => {
    if (role !== "operator") return;
    api.settings().then((s) => {
      if (s.setup_needed && location.pathname !== "/app/setup") navigate("/app/setup", { replace: true });
    }).catch(() => {});   // settings unavailable: the rest of the app is unaffected
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [role]);
  const pendingCount = (inbox.windows || []).filter((w) => w.undecided > 0).length;

  return (
    <div className="shell">
      <header className="shell-header">
        <div className="shell-brand">
          <span className="shell-mark" aria-hidden="true" />
          Bird Monitoring
        </div>

        <button
          className="shell-menu-toggle"
          aria-expanded={menuOpen}
          aria-controls="shell-nav"
          onClick={() => setMenuOpen((v) => !v)}
        >
          Menu
        </button>

        <nav id="shell-nav" className={`shell-nav ${menuOpen ? "is-open" : ""}`}>
          {CLIENT_NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `shell-link ${isActive ? "is-active" : ""}`}
              onClick={() => setMenuOpen(false)}
            >
              {item.label}
            </NavLink>
          ))}
          {role === "operator" && (
            <>
              <span className="shell-nav-sep" aria-hidden="true" />
              {OPERATOR_LINKS.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) => `shell-link ${isActive ? "is-active" : ""}`}
                  onClick={() => setMenuOpen(false)}
                >
                  {item.label}
                  {item.badge && pendingCount > 0 && <span className="shell-badge">{pendingCount}</span>}
                </NavLink>
              ))}
            </>
          )}
        </nav>

        <div className="shell-user">
          <AcquisitionButton
            status={acquisition.status} error={acquisition.error}
            reconnecting={acquisition.reconnecting} busy={acquisition.busy}
            onToggle={acquisition.setEnabled}
          />
          <NotificationBell events={events} onDismiss={dismiss} onClearAll={clearAll} />
          <span className="shell-role" title={`Signed in as ${username}`}>
            {role}
          </span>
          <button type="button" className="shell-logout" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <main className="shell-content">
        <Outlet />
      </main>
    </div>
  );
}
