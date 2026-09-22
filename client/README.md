# client — the client-facing React app

Serves Live / Sightings / Species (and, for `operator`-role sessions, links
out to Review/Dataset). See [MILESTONES.md](../../MILESTONES.md) for what's
built vs. planned, and [ARCHITECTURE.md](../../ARCHITECTURE.md) for why the
app is shaped this way.

`backend/app.py` serves this app's built files directly — there's no
separate server or port for it in normal use. Node is only needed to build
it; the running app never calls `node`/`npm`.

## Requirements

Node.js, if not already installed:

```bash
conda create -n birdclient -c conda-forge nodejs=20 -y
conda activate birdclient
```

## First-time setup

```bash
cd bird-review-app/client
npm install
npm run build          # writes dist/ -- backend/app.py serves this
```

You also need a users file before login works — see the top-level README's
"What you need" section (`users.yaml`, added with `backend/auth.py`).

Then run `backend/app.py` as usual (top-level README).

## Rebuilding after a change

```bash
npm run build
```

No server restart needed — just a browser refresh.

## Local development (optional)

```bash
npm run dev
```

Vite's dev server with hot reload on port 5173, proxying `/api` and
`/stream` to `backend/app.py` on `127.0.0.1:8766` (see `vite.config.js`), so
you can develop against real data without rebuilding on every change. Local
iteration only — the running app never serves this dev server, only the
`dist/` build.

**Known issue, dev server only:** `npm audit` flags the pinned Vite/esbuild
version for a dev-server-only advisory. Doesn't affect the production build.
Not upgraded yet — the fix is a Vite 6→8 major bump.

## Layout

```
src/
  main.jsx           entry point, router + AuthProvider
  App.jsx            routes -- see below
  api.js             fetch wrapper (session cookie, ApiError vs network error)
  AuthContext.jsx     loading / anonymous / authenticated / offline session state
  hooks/useCameras.js shared polling of /api/cameras (one place to swap for push later)
  lib/grouping.js    groups camera rows by site and by mast (the `pair` field)
  pages/             one file per route
  components/        Shell (nav), MastMap (schematic layout), CameraCard,
                     StatusPill, ComingSoon
  styles/            tokens.css (design tokens), global.css (resets + shared utility classes)
```

Routes:

| Path | Page | Notes |
|---|---|---|
| `/login` | Login | |
| `/app/live` | SitesList | one card per site, health summary |
| `/app/live/:site` | SiteDetail | schematic mast layout + camera tiles |
| `/app/live/:site/camera/:name` | CameraDetail | single camera, larger view |
| `/app/sightings` | Sightings | confirmed-sighting gallery, filterable |
| `/app/species` | Species | per-species stats + gallery |

See [MILESTONES.md](../../MILESTONES.md) for why this hierarchy was chosen
over a flat camera grid.
