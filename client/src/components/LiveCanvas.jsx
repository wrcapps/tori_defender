import React, { useEffect, useRef, useState } from "react";
import { subscribeLive } from "../lib/liveWall.js";
import "./LiveCanvas.css";

// A live camera picture drawn from the shared multiplexed stream (lib/liveWall.js)
// instead of an <img src=/stream/...>, so any number of tiles share one connection.
export default function LiveCanvas({ camera, alt, onFirstFrame }) {
  const ref = useRef(null);
  const [status, setStatus] = useState("connecting");

  useEffect(() => {
    let first = true;
    return subscribeLive(
      camera,
      (bmp) => {
        const c = ref.current;
        if (!c) return;
        if (c.width !== bmp.width || c.height !== bmp.height) {
          c.width = bmp.width; c.height = bmp.height;
        }
        c.getContext("2d").drawImage(bmp, 0, 0);
        if (first) { first = false; setStatus("live"); onFirstFrame?.(); }
      },
      (s) => { if (s !== "open") setStatus(s); },
    );
  }, [camera]);

  return (
    <>
      <canvas ref={ref} role="img" aria-label={alt} />
      {status !== "live" && (
        <span className="live-canvas-state">
          {status === "reconnecting" ? "Reconnecting…" : "Connecting…"}
        </span>
      )}
    </>
  );
}
