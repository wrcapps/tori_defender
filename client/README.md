# client — the client-facing React app

Serves Live / Sightings / Species (and, for `operator`-role sessions, links
out to the existing Review/Dataset pages). See [MILESTONES.md](../../MILESTONES.md)
for what's built vs. planned, and [ARCHITECTURE.md](../../ARCHITECTURE.md) for
why the app is shaped this way.

`app.py` (the Python backend, one directory up) serves this app's production
build directly — there is no separate server or port for it. `app.py` itself
requires Node only to have been run once, at build time; it never invokes
`node`/`npm` at runtime.

## Requirements

Node.js is not otherwise used by this project. If it isn't installed:

```bash
conda create -n birdclient -c conda-forge nodejs=20 -y
conda activate birdclient
```

## First-time setup

```bash
cd bird-review-app/client
npm install
npm run build          # writes dist/ -- app.py serves this at /login and /app/*
```

`app.py` also needs a users file before login works at all:

```bash
cd bird-review-app
cp users.example.yaml users.yaml && chmod 600 users.yaml
python auth.py --users users.yaml --add-user alice --role operator
python auth.py --users users.yaml --add-user acme-corp --role client
```

Then run `app.py` as usual (see the top-level README) — `--users users.yaml`
is the default path, so no new flag is needed unless the file lives
elsewhere.

## Rebuilding after a change

```bash
npm run build
```

`app.py` reads the built files from disk on every request — no server
restart needed after a rebuild, just a browser refresh.

## Local development (optional)

```bash
npm run dev
```

Runs Vite's dev server with hot reload on port 5173, proxying `/api` and
`/stream` to `app.py` on `127.0.0.1:8766` (see `vite.config.js`) so the app
can be developed against real data without rebuilding on every change. This
is for local iteration only — `app.py` never serves this dev server; it only
ever serves the `dist/` build.

**Known issue, dev server only:** `npm audit` flags the pinned Vite/esbuild
version for a dev-server-only advisory (a page could make the dev server
echo back arbitrary file contents while `npm run dev` is running). It does
not affect the production build `app.py` serves, since that build contains
no dev server. Not upgraded yet because the fix is a Vite 6→8 major version
bump; revisit before this becomes a persistent, always-on dev environment
rather than an occasional local one.

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

See [MILESTONES.md](../../MILESTONES.md) for why this hierarchy (rather than
a flat camera grid) was chosen, and what was deliberately *not* carried over
from the reference interface it was modeled on.
