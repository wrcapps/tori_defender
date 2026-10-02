# Bird Review App

This app watches bird cameras and helps you check what it found.

- **Live** – see the cameras and switch recording on or off.
- **Review** – look at the birds the app detected. Say "yes, a bird" or "no, a mistake".
- **Sightings / Species** – see what was found and how many.

---

## 1. Install (once)

### On Windows

1. Install **Python** from python.org. During install, tick **"Add Python to PATH"**.
2. Open the app folder. Right-click an empty spot, choose **"Open in Terminal"**, and paste:

   ```
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   It asks a few questions (which site, the NAS address, your NAS login). Just answer them.
3. Copy the **model file** you were given into the **`models`** folder.
4. Create your login. In the same terminal, paste this (change `alice` to your name) and type a password when asked:

   ```
   .venv\Scripts\python.exe backend\auth.py --users users.yaml --add-user alice --role operator
   ```

### On Linux

Open a terminal in the app folder and run `./install.sh`. It asks for the NAS login and does the rest.
Then copy your model file into the `models` folder and create your login:

```
source .venv/bin/activate
python backend/auth.py --users users.yaml --add-user alice --role operator
```

---

## 2. Start the app

- **Windows:** double-click **`start.bat`**.
- **Linux:** `./backend/app.py --config config.yaml`, then open **http://127.0.0.1:8766** in your browser.

Sign in with the name and password you created.

---

## 3. First-time setup

The first time you sign in, the app asks two things:

1. **Model** – pick one of the files you put in the `models` folder.
2. **Data folder** – the NAS folder where confirmed footage is archived (the app works on its own disk and moves finished data there). Examples:
   - Windows: `Z:\Dataset` or `\\192.168.1.138\Pasari\Dataset`
   - Linux: the **`tori_sites`** folder inside the app folder (`install.sh` created it for you)

   The app tells you right away if the folder cannot be found.

You can change these later: click **Settings** in the top menu.
A new model works right away. A new data folder works after you close and open the app again.

**There is nothing to choose about the graphics card.** If the computer has an NVIDIA GPU the app uses it
(for detection and for decoding the camera video); if not, it runs on the CPU by itself.

---

## 4. Using the app

### Live
Tick a camera to watch it. Press **record** to also save what it sees. Saved clips show up in Review.

### Review
Pick a session on the left, then go through the pictures. A box is something the app thinks is a bird. Judge each box:

| Key | What it does |
|---|---|
| `←` `→` | previous / next picture |
| `Y` | yes, it is a bird |
| `N` | no, it is a mistake |
| `space` | not sure |
| `U` | jump to the next box you have not judged |
| `P` | draw a box the app missed |
| `Del` | delete the selected box |
| `Z` | undo |
| `+` `-` `0` | zoom in, zoom out, fit |

When you have finished a session, click **done**.

---

## Accounts

Users cannot sign themselves up: the administrator creates each account on the computer where the app runs.
Run this (change `alice`; use `--role client` for a read-only account) and type the password twice:

```
python backend/auth.py --users users.yaml --add-user alice --role operator
```

(On Windows: `.venv\Scripts\python.exe backend\auth.py ...`.) Then restart the app so it reads the new account.
Running the same command again for an existing name sets a new password. Accounts are kept in `users.yaml`.

---

## Connecting your cameras

**During install** the script asks "Connect cameras now?": answer `y`, give the cameras' login and their IP
addresses (separated by commas or spaces), and it writes `config.yaml` for you. Add `=name` to name a camera
(`192.168.88.41=mast1-a`); otherwise it is called `cam-41` after the last number. If you skipped it, or a
camera changes later, edit `config.yaml` by hand (there is no screen for it yet):

1. Copy `config.example.yaml` over `config.yaml` (keep a copy of your site name) and open it in a text editor.
2. Set `site:` (a short lowercase name, e.g. `corbu`), and under `defaults:` the camera login (`username`, `password`).
3. Under `cameras:` add one entry per camera, with a `name` (letters, digits, `-`, `_`) and its IP address as `host`:

   ```yaml
   cameras:
     - name: mast1-a
       host: 192.168.88.41
     - name: mast1-b
       host: 192.168.88.42
   ```

   The default stream address is for Hikvision-type cameras (`rtsp://{host}:554/Streaming/Channels/101`, the main stream).
   Other brands: change `record_url` under `defaults:`.
4. Restart the app. The cameras appear in **Live**. Do not share `config.yaml`: it holds the camera passwords.

---

## If something goes wrong

| Problem | What to do |
|---|---|
| Detection is slow and the computer has an NVIDIA card | The card is not being used. Re-run the installer (it checks), or see "GPU and the install scripts" below. |
| Folder "does not exist" | The NAS is not connected. Connect it, then try again. |
| Cameras show video but no boxes | No model is chosen. Open **Settings** and pick one. |
| Detection is very slow | The computer has no usable GPU, so it runs on the CPU. This is normal; use a computer with an NVIDIA GPU for many cameras. |
| Cannot sign in | Ask your administrator to create your login (step 1, point 4). |

---

## GPU and the install scripts

`install.sh` (Linux) and `install.ps1` (Windows) look for an NVIDIA GPU (`nvidia-smi`):

- **GPU found:** they check that PyTorch can use it (Windows offers the CUDA build) and, if `ffmpeg` is installed, add
  `PyNvVideoCodec` (`requirements-gpu.txt`) so camera video is decoded by the GPU instead of the CPU. Twelve 4K cameras
  need about 14 CPU cores to decode, and about 0.15 core on the GPU.
- **No GPU, or ffmpeg missing:** nothing breaks. The app decodes and detects on the CPU. To add GPU decoding later:
  `pip install --no-deps -r requirements-gpu.txt`.

The service log says which one is in use ("NVDEC decode: available" or "not used (...)"), and
`logs/acquisition/status.json` shows `decoders: {nvdec: N, cpu: M}`. `live.decoder: cpu` in `config.yaml` forces the CPU.

## For technical staff

- Details on disk layout, GPU and bandwidth planning: [docs/DATA_LAYOUT.md](docs/DATA_LAYOUT.md), [docs/ADVANCED.md](docs/ADVANCED.md).
- Own cameras: copy `config.example.yaml` to `config.yaml` and fill in the site name, the cameras and their passwords.
- Choices made in the app (model, data folder) are saved in `settings.yaml` (next to the app; a `device:` key from an older version is ignored). Without that file, the app uses `config.yaml`
  and `--weights`. `--archive-root` on the command line overrides the data folder from Settings; `--sites-root` moves the local working tree (default `sites/` next to the app).
- Capture and review work on the local disk; confirmed windows and negatives older than 24 h are moved to the data folder (the NAS) in the background, see [docs/DATA_LAYOUT.md](docs/DATA_LAYOUT.md) §0.
- Two roles: **operator** (everything) and **client** (Live, Sightings, Species; read-only). Add `--role client` to give a read-only login.
- Packaged Windows app (`BirdReview.exe`): see [desktop/README.md](desktop/README.md).
- Training-label audit: start with `--dataset path/to/data.yaml`.
