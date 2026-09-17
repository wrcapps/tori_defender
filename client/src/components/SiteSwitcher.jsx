import React from "react";
import "./SiteSwitcher.css";

export default function SiteSwitcher({ sites, value, onChange }) {
  if (!sites || sites.length <= 1) return null;
  return (
    <div className="site-switcher" role="tablist" aria-label="Site">
      {sites.map((site) => (
        <button
          key={site}
          type="button"
          role="tab"
          aria-selected={site === value}
          className={`site-switcher-btn ${site === value ? "is-active" : ""}`}
          onClick={() => onChange(site)}
        >
          {site}
        </button>
      ))}
    </div>
  );
}
