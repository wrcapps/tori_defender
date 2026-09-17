import React, { useEffect, useRef } from "react";

const MAG_NATIVE = 224; // matches the saved crops, so magnifier and crops agree
const MAG_OUT = 260;

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// Draws a zoomed patch of the currently-loaded frame image around one point,
// with detection boxes outlined on top -- native pixels, not the (possibly
// downscaled) displayed image, so a 10-45px bird is actually legible.
export default function Magnifier({ imgRef, nativeW, point, boxes, hint }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, MAG_OUT, MAG_OUT);
    if (!point || !nativeW || !img || !img.naturalWidth) return;

    const k = img.naturalWidth / nativeW;
    const span = MAG_NATIVE * k;
    const sx = clamp(point.cx * k - span / 2, 0, Math.max(0, img.naturalWidth - span));
    const sy = clamp(point.cy * k - span / 2, 0, Math.max(0, img.naturalHeight - span));
    ctx.drawImage(img, sx, sy, span, span, 0, 0, MAG_OUT, MAG_OUT);
    const z = MAG_OUT / span;
    (boxes || []).forEach(({ bbox, color }) => {
      const [x0, y0, x1, y1] = bbox;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.strokeRect((x0 * k - sx) * z, (y0 * k - sy) * z, (x1 - x0) * k * z, (y1 - y0) * k * z);
    });
  }, [imgRef, nativeW, point, boxes]);

  return (
    <section className="review-section">
      <h2>Magnifier</h2>
      <canvas ref={canvasRef} width={MAG_OUT} height={MAG_OUT} />
      <div className="review-hint">{hint}</div>
    </section>
  );
}
