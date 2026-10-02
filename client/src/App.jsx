import React from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./AuthContext.jsx";
import Login from "./pages/Login.jsx";
import Shell from "./components/Shell.jsx";
import SitesList from "./pages/SitesList.jsx";
import SiteDetail from "./pages/SiteDetail.jsx";
import CameraDetail from "./pages/CameraDetail.jsx";
import Matrix from "./pages/Matrix.jsx";
import Sightings from "./pages/Sightings.jsx";
import Species from "./pages/Species.jsx";
import Review from "./pages/Review/Review.jsx";
import Dataset from "./pages/Dataset.jsx";
import TrackView from "./pages/TrackView.jsx";
import Inbox from "./pages/Inbox.jsx";

function RequireAuth({ children }) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") return <FullPageState label="Loading…" />;
  if (status === "offline") {
    return <FullPageState label="Can't reach the server. Retrying…" tone="warn" />;
  }
  if (status === "anonymous") {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace />;
  }
  return children;
}

// Review (box editing/verdicts) and Dataset (label audit) are mutating,
// operator-only tools -- unlike the legacy static pages they replace (which
// let any authenticated session view them and only refused their POSTs),
// this guard closes that gap at the route level too, not just server-side.
function RequireOperator({ children }) {
  const { role } = useAuth();
  if (role !== "operator") return <Navigate to="/app/live" replace />;
  return children;
}

function FullPageState({ label, tone }) {
  return (
    <div style={{
      minHeight: "100%", display: "flex", alignItems: "center", justifyContent: "center",
      color: tone === "warn" ? "var(--warn)" : "var(--text-dim)", fontSize: 14,
    }}>
      {label}
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/app"
          element={
            <RequireAuth>
              <Shell />
            </RequireAuth>
          }
        >
          <Route index element={<Navigate to="live" replace />} />
          <Route path="live" element={<SitesList />} />
          <Route path="live/:site" element={<SiteDetail />} />
          <Route path="live/:site/matrix" element={<Matrix />} />
          <Route path="live/:site/camera/:name" element={<CameraDetail />} />
          <Route path="live/:site/track/:trackId" element={<TrackView />} />
          <Route path="sightings" element={<Sightings />} />
          <Route path="species" element={<Species />} />
          <Route path="inbox" element={<RequireOperator><Inbox /></RequireOperator>} />
          <Route path="review" element={<RequireOperator><Review /></RequireOperator>} />
          <Route path="dataset" element={<RequireOperator><Dataset /></RequireOperator>} />
          <Route path="*" element={<Navigate to="live" replace />} />
        </Route>
        <Route path="*" element={<Navigate to="/app/live" replace />} />
      </Routes>
    </AuthProvider>
  );
}
