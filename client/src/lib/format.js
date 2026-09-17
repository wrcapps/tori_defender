// A window folder is named "<camera->?HHMMSS" (see camera_for_window in
// app.py) -- the trailing 6 digits are always the real capture start time,
// never a display placeholder, so this is exact, not approximate.
export function windowTime(window) {
  const match = /(\d{2})(\d{2})(\d{2})$/.exec(window);
  return match ? `${match[1]}:${match[2]}:${match[3]}` : null;
}

export function formatWhen(day, window) {
  const time = windowTime(window);
  return time ? `${day} · ${time}` : day;
}

export function formatConfidence(conf) {
  return `${Math.round(conf * 100)}%`;
}
