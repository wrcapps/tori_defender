import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api.js";

// Settings (model, data folder) as the server reports them, plus an editable draft.
// Nothing is optimistic: what the page shows as "current" is always the server's last answer;
// the draft is only what the person has typed/clicked since.
const FIELDS = ["model", "data_dir"];

function draftFrom(state) {
  const s = state.settings;
  // First run with models available and none in use yet: preselect the only/first one, so the
  // common case (one model shipped in models/) is a single click on Next.
  const model = s.model ?? (state.effective_model || !state.models.length ? "" : state.models[0].file);
  return { model: model || "", data_dir: s.data_dir || "" };
}

export function useSettings() {
  const [state, setState] = useState(null);       // null until the first answer
  const [draft, setDraft] = useState(null);
  const [error, setError] = useState(null);       // load error
  const [saveError, setSaveError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState(null);

  const load = useCallback(async () => {
    try {
      const next = await api.settings();
      setState(next);
      setDraft((d) => d || draftFrom(next));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server. Retrying…");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // A failed first load retries by itself: the page must recover without a manual reload.
  useEffect(() => {
    if (!error || state) return undefined;
    const id = setTimeout(load, 3000);
    return () => clearTimeout(id);
  }, [error, state, load]);

  const edit = useCallback((field, value) => {
    setSavedNote(null);
    setSaveError(null);
    setDraft((d) => ({ ...d, [field]: value }));
  }, []);

  const changes = useMemo(() => {
    if (!state || !draft) return {};
    const cur = draftFrom({ ...state, effective_model: null, models: [] });   // what is saved, unadjusted
    const out = {};
    for (const f of FIELDS) {
      if ((draft[f] || "") !== (cur[f] || "")) out[f] = draft[f] || null;
    }
    return out;
  }, [state, draft]);

  const save = useCallback(async (extra = {}) => {
    setSaving(true);
    setSaveError(null);
    try {
      const next = await api.saveSettings({ ...changes, ...extra });
      setState(next);
      setDraft(draftFrom(next));
      const parts = [];
      if ("model" in changes) {
        parts.push(next.apply_error
          ? `Saved, but acquisition could not restart: ${next.apply_error}`
          : "Saved. The model applies now (acquisition restarts if it was running).");
      }
      if (next.data_dir.restart_needed) parts.push("The data folder applies the next time the app starts.");
      setSavedNote(parts.join(" ") || "Saved.");
      return next;
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Can't reach the server — nothing was saved.");
      return null;
    } finally {
      setSaving(false);
    }
  }, [changes]);

  return { state, draft, error, edit, changes, dirty: Object.keys(changes).length > 0,
           save, saving, saveError, savedNote };
}

// Debounced "is this folder usable?" check for the data-folder field.
export function useDataDirCheck(path) {
  const [result, setResult] = useState(null);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!path.trim()) {
      setResult(null);
      setFailed(false);
      return undefined;
    }
    setChecking(true);
    let stale = false;
    const timer = setTimeout(async () => {
      try {
        const r = await api.checkDataDir(path);
        if (!stale) { setResult(r); setFailed(false); }
      } catch {
        if (!stale) { setResult(null); setFailed(true); }
      } finally {
        if (!stale) setChecking(false);
      }
    }, 450);
    return () => { stale = true; clearTimeout(timer); };
  }, [path]);
  return { result, checking, failed };
}
