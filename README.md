# bird-review-app

Live bird detection from IP cameras, and a web app for reviewing what the
model found.

- **Review** — go through captured sessions, confirm or reject detections,
  draw in what the model missed.
- **Live** — watch cameras across every site and turn recording on/off.
- **Dataset** — audit the training labels themselves (optional).

---

## What you need

There are two ways to use this app. Pick the one that matches you.

| | Reviewer (no cameras of your own) | Full setup (your own cameras) |
|---|---|---|
| You need | Access to the shared NAS with existing footage | The cameras themselves, plus a model weights file |
| Setup script | `./install.sh` (does steps below for you) | Manual, see below |

Either way, you'll end up with these config files (copy the `.example` files
and edit them — real copies hold passwords and stay out of git):

| File | What it's for |
|---|---|
| `users.yaml` | Who can log into the app, and with what role (`operator` or `client`). Copy from `users.example.yaml`. |
| `config.yaml` | Which site this is, the camera list and their passwords, and the model to use. Copy from `config.example.yaml`. |
| `nas.env` *(reviewer only)* | Address of the shared NAS. Copy from `nas.example.env` — optional, `install.sh` will ask for the same things interactively if it's missing. |

---

## Install & run

### Reviewer — already-captured data on the shared NAS

```bash
./install.sh
```

Asks for the NAS login (given to you separately), mounts it, sets up the
Python environment, and writes a minimal `config.yaml` for you. Then:

```bash
source .venv/bin/activate
cp users.example.yaml users.yaml && chmod 600 users.yaml
python backend/auth.py --users users.yaml --add-user alice --role client
./backend/app.py --config config.yaml --sites-root "$(pwd)/tori_sites"
```

### Full setup — your own cameras

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml && chmod 600 config.yaml
# edit config.yaml: site name, camera list, camera passwords, model weights

cp users.example.yaml users.yaml && chmod 600 users.yaml
python backend/auth.py --users users.yaml --add-user alice --role operator

./backend/app.py --config config.yaml --weights weights/best.pt
```

### Either way

Open **http://127.0.0.1:8766/login** (on a remote machine, forward the port —
VS Code's PORTS panel, or `ssh -L 8766:127.0.0.1:8766 user@host`).

Two login roles:

- **operator** — everything: Review, Live with recording, Dataset audit.
- **client** — read-only: Live (no recording), Sightings, Species.

Add more sites by passing `--config` more than once (camera names must stay
unique across all of them). Each site keeps its own data, switchable from a
site picker in the app.

---

## Using the app

### Review

Pick a site and a session on the left, then step through frames in the
middle. Boxes are the model's detections; judge each one.

| key | |
|---|---|
| `←` `→` | step one frame (`shift` = ten) |
| `Y` / `N` / `space` | real bird / false positive / unsure |
| `U` | jump to the next un-judged detection |
| `P` | add a box the model missed |
| `M` | move a box |
| `F` | follow a box onto the next frames (drag to correct) |
| `X` | stop following |
| `Del` | delete the selected box |
| `Z` | undo the last verdict |
| `+` `-` `0` | zoom in, out, fit |

Species, distance and size notes are optional, and attach to whichever box
is selected. Click **done** on a session once you've been through it.

### Live

Tick a camera to start watching it — the app starts capture for you, no need
to run anything by hand. Click **record** to also start saving what it sees
(off by default); those recordings show up in Review. A site with more
cameras than the GPU can run at once will warn you before starting one more
than fits.

### Dataset

Start the app with `--dataset path/to/data.yaml` to audit training labels,
worst-looking first (out of bounds, too tiny, too big, then the rest). Reject
a label by clicking its cell; `./backend/apply_label_review.py --data
path/to/data.yaml` then writes a cleaned copy, leaving the original untouched.

---

## More detail

GPU/bandwidth/disk budgeting, running capture without the app, and where
files end up on disk are in [docs/ADVANCED.md](docs/ADVANCED.md).
