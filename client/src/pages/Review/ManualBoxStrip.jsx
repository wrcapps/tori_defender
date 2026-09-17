import React from "react";

export default function ManualBoxStrip({ site, boxes }) {
  const withCrop = boxes.filter((b) => b.crop);
  if (!withCrop.length) return null;
  return (
    <section className="review-section">
      <h2>Manual boxes on this frame</h2>
      <div className="review-strip">
        {withCrop.map((b) => (
          <img key={b.id} src={`/img/${site}/${b.crop}`} alt="manual box" />
        ))}
      </div>
    </section>
  );
}
