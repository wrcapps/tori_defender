import React, { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../AuthContext.jsx";
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
  { to: "/app/review", label: "Review" },
  { to: "/app/dataset", label: "Dataset" },
];

export default function Shell() {
  const { username, role, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

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
                </NavLink>
              ))}
            </>
          )}
        </nav>

        <div className="shell-user">
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
