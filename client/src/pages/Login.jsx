import React, { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../AuthContext.jsx";
import "./Login.css";

export default function Login() {
  const { status, role, login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  if (status === "authenticated") {
    return <Navigate to={safeNext(location.search) || defaultHomeFor(role)} replace />;
  }

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await login(username.trim(), password);
      navigate(safeNext(location.search) || defaultHomeFor(result.role), { replace: true });
    } catch (err) {
      setError(err.message === "network-error"
        ? "Can't reach the server right now. Check your connection and try again."
        : "Incorrect username or password.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-brand">
          <span className="login-mark" aria-hidden="true" />
          <span className="login-title">Bird Monitoring</span>
        </div>
        <p className="login-sub">Sign in to view live cameras and sightings.</p>

        <form onSubmit={onSubmit} noValidate>
          <label className="field">
            <span>Username</span>
            <input
              type="text"
              autoComplete="username"
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <label className="field">
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>

          <div role="alert" aria-live="polite" className="login-error">
            {error}
          </div>

          <button type="submit" className="login-submit" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}

export function defaultHomeFor(role) {
  return role === "operator" ? "/app/live" : "/app/live";
}

// Pattern-matched against this app's actual route shapes, not a prefix
// check: react-router has shipped open-redirect bugs before (a backslash in
// a path being browser-normalized into a protocol-relative URL -- the
// dependency is pinned to a patched version for that specific CVE, but this
// is cheap defense in depth regardless). Every pattern requires a single
// leading "/" and forbids "/" and "\" *within* a segment, which also rules
// out a "//evil.com" or "\evil.com" value ever reaching <Navigate>/navigate().
const SAFE_NEXT_PATTERNS = [
  /^\/app\/live$/,
  /^\/app\/live\/[^/\\]+$/,
  /^\/app\/live\/[^/\\]+\/matrix$/,
  /^\/app\/live\/[^/\\]+\/camera\/[^/\\]+$/,
  /^\/app\/sightings$/,
  /^\/app\/species$/,
];

function safeNext(search) {
  const next = new URLSearchParams(search).get("next");
  return next && SAFE_NEXT_PATTERNS.some((re) => re.test(next)) ? next : null;
}
