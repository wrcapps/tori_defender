import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "./api.js";

const AuthContext = createContext(null);

// Three states, not two -- "loading" (we haven't asked /api/whoami yet) is
// distinct from "anonymous" (we asked, and there is no session). Collapsing
// them would flash a login prompt for a fraction of a second on every page
// load even for an already-logged-in operator, which reads as broken.
export function AuthProvider({ children }) {
  const [state, setState] = useState({ status: "loading", username: null, role: null });

  const refresh = useCallback(async () => {
    try {
      const who = await api.whoami();
      setState(who.role
        ? { status: "authenticated", username: who.username, role: who.role }
        : { status: "anonymous", username: null, role: null });
    } catch {
      // /api/whoami itself failed to answer at all (server down, network
      // drop) -- distinct from a clean "no session" response.
      setState({ status: "offline", username: null, role: null });
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // api.js dispatches this the moment ANY call comes back 401 -- catches a
  // session dying while the SPA is already open and running (not just at
  // the next full page load), so a stale page doesn't sit there quietly
  // showing broken/empty data from every failed fetch. RequireAuth (App.jsx)
  // redirects to /login the instant `status` flips to "anonymous", no
  // refresh needed.
  useEffect(() => {
    function onExpired() {
      setState((prev) => (prev.status === "authenticated"
        ? { status: "anonymous", username: null, role: null } : prev));
    }
    addEventListener("auth:expired", onExpired);
    return () => removeEventListener("auth:expired", onExpired);
  }, []);

  const login = useCallback(async (username, password) => {
    const result = await api.login(username, password);
    setState({ status: "authenticated", username: result.username, role: result.role });
    return result;
  }, []);

  const logout = useCallback(async () => {
    await api.logout().catch(() => {});
    setState({ status: "anonymous", username: null, role: null });
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
