**English** | [Português](README.pt-BR.md)

# counter_vision

Multi-camera **people counting + dwell detection** for a physical-retail service
point, in two tiers:

- **Edge unit** — one movable enclosure (Raspberry Pi 5 + AI HAT+ / Hailo + wide
  CSI cameras) detects, tracks and counts **footfall** (people crossing a tripwire)
  and **stops** (people who dwell ≥ N seconds in a zone), with faces **pixelated on
  the device** before anything is saved (LGPD).
- **Central hub (VPS)** — a FastAPI + SQLite service receives the anonymous counts
  (and, in pilot, re-ID vectors + pixelated snapshots) and serves a **hosted
  geometry/config editor** and a **conversion dashboard**. The board **pulls its
  config from the hub on boot**.

One question per service point: **of the people who pass, how many stop — and for
how long?** (conversion = stops ÷ passers).

**Phase-1 build: 2× wide CSI cameras → ~180°** (unit against a wall/counter). The
Pi 5 has only 2 CSI ports, so 2 is the native simultaneous max. The code is
**N-camera** throughout, so a later **360°** deployment (4 cameras) is a hardware +
config change, not a rewrite. See "Future 360°".

## Architecture

```
EDGE UNIT — Raspberry Pi 5 + AI HAT+ (Hailo) + 2 wide CSI cams (~180°)
   per camera, independently:
   capture ─► person detect (Hailo) ─► track (ByteTrack)
        │   counting geometry (pulled from the hub), per camera:
        ▼   tripwire → footfall      zone + dwell ≥ N s → stop
   faces pixelated on-device (LGPD) → local SQLite + snapshots + re-ID vectors
        │
        │   count.py + upload_to_server.py  — systemd, auto-restart
        ▼   HTTPS + Bearer token, OUTBOUND ONLY (board behind NAT)
CENTRAL HUB — VPS: FastAPI + SQLite behind nginx + Let's Encrypt
   ├─ /config   hosted geometry + parameter editor → board pulls on boot
   ├─ /         conversion dashboard (date range · granularity · place ·
   │            images/vectors · pagination · freshness · downloads · purge)
   └─ /api/*    events · vectors · snapshots · frames · config · capture
```

## How counting works (per-camera sectors, not multi-view fusion)

The cameras watch *different* sectors (2 across ~180° now; up to 4 for 360°
later), so a person is normally in one camera's view at a time — no need to fuse
simultaneous views of the same spot. Each camera runs its own pipeline and counts
using geometry drawn in its own image:

- **Footfall** — a person crossing a **tripwire line** (entering the area).
- **Stops** — a person dwelling ≥ N seconds inside a **zone** (a service point).
  We also record **how long** each person stays (dwell duration), not just a
  binary stop → avg/median/longest/distribution of time at the counter.

Per-camera counts are aggregated. This deliberately drops the earlier homography /
world-coordinate fusion: that needed fixed, calibrated cameras and bought
occlusion-robustness we don't need for outward-facing coverage. Trade-off: an
outward fan won't see *around* people at a dense single counter (each spot is seen
by ~1 camera).

### How double-counting is prevented (dedup by geometry, not by re-ID)

Cameras overlap in *view* (required to tile 360°/180° without gaps), so a person
in an overlap is *seen* by 2 cameras. The fix is to count **line crossings**, not
presence, and to make the counting geometry **non-overlapping by construction**:

- The counting boundary is **one ring** (360°) or **one line** (180°) split into
  **disjoint arcs/segments, one per camera**. Crossing it fires exactly one count,
  owned by the single arc crossed — regardless of how many cameras see the person.
- **Directional crossing (in/out) + per-track-ID cooldown** so wobble on the line,
  or walking straight through (1 in + 1 out), nets correctly.
- Each **dwell zone is owned by exactly one camera**. A service point in an overlap
  goes to the better-viewing camera; the other ignores it.
- Only genuinely seam-straddling cases need a fuzzy **boundary hand-off** (match a
  track leaving cam A's seam to one entering cam B within a short time window via a
  cheap color/appearance signature) — optional. No full cross-camera re-ID.

The hosted geometry editor enforces the one invariant: **lines/zones must not
overlap in coverage.** That single rule is what guarantees no double counting.

## Privacy & LGPD

Personal data (faces, re-ID vectors, images) is minimized and tightly scoped:

- **Faces pixelated on the device** before any frame is saved or uploaded — no raw
  face ever leaves the board.
- **Counts are anonymous aggregates** (pass/stop events; no identity).
- **Re-ID (pilot) is a clothing/body colour signature, not biometric** — opt-in,
  matched only within a short window, auto-erased after a retention period;
  face/full (biometric) modes are gated.
- **Data purge**: the dashboard erases vectors + pixelated images for a chosen date
  range; the server also auto-deletes both after `COUNTER_RETAIN_DAYS`.
- **Per-device "place" label** is logged on every event/vector/image, so location
  history is preserved when the movable unit is relocated.
- Token never in the repo (env / `~/.counter_token`); TLS + HSTS everywhere;
  backend bound to localhost behind nginx. A posted notice at the counter + a
  documented retention policy are part of the deployment.

## The edge unit (board-agnostic by design)

The code abstracts the two hardware-specific layers so the detector/camera choice
can change without touching the pipeline:

- **Camera source** (`src/cameras/`): `USBCamera` (OpenCV) for laptop dev, and
  `Picamera2Camera` (CSI, on the Pi). Both are `CameraSource` subclasses; the
  pipeline never sees the difference.
- **Detector** (`src/detect/`): pluggable backend — `cpu` (Ultralytics, for laptop
  dev) and `hailo` (HailoRT on the AI HAT+, production on the Pi).

**Hardware: one tethered movable unit = Raspberry Pi 5 + AI HAT+ 13 TOPS
(Hailo-8L) + 2× wide CSI cameras**, in a 3D-printed case. The Hailo runs YOLOv8
person detection on both streams in real time with huge headroom — enough to run a
**larger/more accurate model** (YOLOv8s/m) since only 2 streams share 13 TOPS. The
HAT is on **PCIe**; the cameras are on **CSI** — so all USB ports stay free and no
hub is needed.

**Cameras: 2× wide CSI modules.** Recommended: **Raspberry Pi Camera Module 3
Wide** (IMX708, ~120° diagonal ≈ ~102° horizontal, autofocus). Two at ~90° apart
cover ~180°. ⚠️ Get the **Wide** variant — the standard Module 3 is only ~66°
horizontal and would leave a gap. Arducam wide IMX219 modules also work.

⚠️ **Pi 5 camera cable:** the Pi 5 uses the **narrow 22-pin** CSI connector, but
cameras ship with a **15-pin** ribbon — buy a **15→22-pin Pi 5 adapter cable** per
camera.

Detector backend = **HailoRT on the AI HAT+**: officially supported on Pi OS via
the **`hailo-all`** apt package (HailoRT + PCIe driver + tooling); use a
**pre-compiled YOLOv8/person `.hef`** from Hailo's model zoo (the Dataflow Compiler
is x86-only if you ever build a custom `.hef`).

### Future 360°

The 2 CSI ports cap the Pi 5 at 2 simultaneous cameras. To reach 360° (4 cameras)
later — **without a code rewrite** (the pipeline is already N-camera) — swap to a
**Compute Module 5 + 4-CSI carrier**, or **import USB camera modules** (Arducam
B0201 / ELP, UVC+MJPEG, ~90–120°, avoid fisheye) and add a powered hub. Design the
3D case with a **swappable front camera mount** so a 4-camera ring can replace the
2-camera front later.

## The central hub (VPS)

A small FastAPI + SQLite service (`server/`) is the hub. Boards reach it **outbound
only** over HTTPS with a Bearer token — the Pi needs no inbound ports or public IP.
It provides:

- **Hosted geometry + config editor** (`/config`) — per camera: draw the **tripwire
  line** + **dwell zone** on a live reference frame (or an **uploaded image**),
  **rotate** to upright (the geometry rotates with it), **collapse** boxes, and set
  parameters (dwell, confidence, re-ID, uploads, **store/place name**, board
  timing) grouped by category with inline hints. **"Take shot"** pulls a fresh frame
  from the board on demand. Saved config — geometry, rotation and parameters — is
  **pulled by the board on its next boot**.
- **Conversion dashboard** (`/`) — passers / stops / conversion aggregated from raw
  events, with a **date-range** filter, **10/30/60-min granularity**, **place**
  filter, per-bucket columns (date, time, place, passers, stops, conversion,
  images, vectors), **pagination**, a **board freshness** heartbeat (last-seen),
  **CSV / images-ZIP** downloads, and a **date-range data purge**.
- **Auth** — a login session cookie for the UI; the Bearer token for the board
  `/api/*` endpoints.

**Deploy:** `server/` ships a `Dockerfile` + `docker-compose.yml` (env
`COUNTER_TOKEN`, `COUNTER_USER`, `COUNTER_PASS`). It listens on localhost; an nginx
vhost (`server/nginx-topofunil.conf`) terminates TLS (Let's Encrypt) + HSTS and
reverse-proxies it. **Rebuild on change with `docker compose up -d --build`** — a
plain `restart` keeps the old image.

## Running

### On the board — production (systemd)

Two **systemd user services** (`systemd/`) auto-start on boot and restart on
failure:
```bash
cp systemd/*.service ~/.config/systemd/user/
loginctl enable-linger                 # start at boot without an interactive login
systemctl --user daemon-reload
systemctl --user enable --now counter counter-upload
journalctl --user -u counter -f        # logs
```
`counter.service` → `count.py` (cameras + Hailo + counting + on-demand frames);
`counter-upload.service` → `upload_to_server.py --loop` (events/vectors/images/frames → hub).

### On the board — manual
```bash
sudo apt install -y python3-picamera2 hailo-all
python3 -m venv --system-site-packages .venv   # so it sees picamera2 + hailo
. .venv/bin/activate
pip install supervision PyYAML opencv-python    # NOT ultralytics — Hailo detects
python count.py config.pi.yaml                  # live: passers / stops / conversion + SQLite log
python report.py                                # dwell stats summary
```

### Laptop dev (no Pi)
```bash
python -m pip install -r requirements.txt       # incl. ultralytics + supervision
python detect_preview.py                        # config.yaml: cam0 = webcam, backend: cpu
```

### The hub (VPS)
```bash
cd server
cp .env.example .env      # COUNTER_TOKEN (openssl rand -hex 32) + COUNTER_USER / COUNTER_PASS
docker compose up -d --build
```

## Build status

- [x] **Edge capture + detection + tracking** — N-camera; Hailo (Pi) / CPU (dev).
- [x] **Counting** — tripwire footfall + zone dwell with per-person duration;
      conversion; events → `data/counts.sqlite`.
- [x] **Hosted geometry + config editor** — draw / rotate / upload-image /
      take-shot; parameters; pulled by the board on boot.
- [x] **Central hub + dashboard** — conversion by date/time/place, images/vectors,
      pagination, freshness, downloads, purge.
- [x] **Privacy / LGPD** — on-device face pixelation, anonymous counts, retention,
      date-range purge.
- [x] **Service-ized** — systemd user services (auto-start + restart-on-failure).
- [~] **Re-ID (pilot)** — clothing/body vectors + ephemeral matching window, under
      validation; biometric modes gated.
- [ ] **Boundary hand-off** for seam-straddling tracks (optional) + **360°**
      (4 cameras via CM5 / USB).
