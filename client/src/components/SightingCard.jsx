import React, { useState } from "react";
import { formatConfidence, formatWhen } from "../lib/format.js";
import "./SightingCard.css";

export default function SightingCard({ site, sighting }) {
  const [imgFailed, setImgFailed] = useState(false);
  const hasImage = sighting.crop && !imgFailed;

  return (
    <div className="sighting-card">
      <div className="sighting-card-media">
        {hasImage ? (
          <img
            src={`/img/${encodeURIComponent(site)}/${sighting.crop}`}
            alt={sighting.species ? `${sighting.species} sighting` : "Unidentified bird sighting"}
            onError={() => setImgFailed(true)}
          />
        ) : (
          <div className="sighting-card-fallback" aria-hidden="true">🕊️</div>
        )}
        <span className="sighting-card-confidence">{formatConfidence(sighting.confidence)}</span>
      </div>
      <div className="sighting-card-body">
        <span className={`sighting-card-species ${!sighting.species ? "is-unidentified" : ""}`}>
          {sighting.species || "Unidentified"}
        </span>
        <span className="sighting-card-when">{formatWhen(sighting.day, sighting.window)}</span>
        <span className="sighting-card-camera">{sighting.camera}</span>
      </div>
    </div>
  );
}
