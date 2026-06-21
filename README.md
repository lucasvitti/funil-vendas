**English** | [Português](README.pt-BR.md)

# counter_vision

Multi-camera **people counting + dwell detection** from a single movable unit.
One enclosure holds a Raspberry Pi 5 + AI HAT+ (Hailo) + wide cameras pointing
**outward** to cover an area. Goal: count **footfall** (people in/around the area)
*and* **stops** (people who dwell ≥ N seconds at a service point).

**Phase-1 build: 2× wide CSI cameras → ~180°** (unit against a wall/counter). The
Pi 5 has only 2 CSI ports, so 2 is the native simultaneous max. The code is
**N-camera** throughout, so a later **360°** deployment (4 cameras) is a hardware +
config change, not a rewrite — via a Compute Module 5 + 4-CSI carrier, or imported
USB camera modules. See "Future 360°" below.

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
  cheap color/appearance signature) — optional, Phase 4. No full cross-camera re-ID.

The Phase 3 geometry editor enforces the one invariant: **lines/zones must not
overlap in coverage.** That single rule is what guarantees no double counting.

```
N cameras (outward: 2→180° now, up to 4→360° later)
        │   per camera, independently:
        ▼
   person detect (Hailo / CPU dev) ─► track (ByteTrack)
        │
        ▼   counting geometry drawn in each camera's image:
   tripwire line → footfall          zone + dwell ≥ N s → stop
        │
        ▼
   aggregate across cameras + boundary dedup at sector overlaps
        │
        ▼
   counts (footfall / stops per interval) + sample snapshots
        │
        ▼
   local SQLite/CSV ──(opt rsync)──► VPS dashboard
```

## Board-agnostic by design

The code abstracts the two hardware-specific layers so the detector/camera choice
can change without touching the pipeline:

- **Camera source** (`src/cameras/`): `USBCamera` (OpenCV) for laptop dev, and
  `Picamera2Camera` (CSI, on the Pi). Both are `CameraSource` subclasses; the
  pipeline never sees the difference.
- **Detector** (`src/detect/`, Phase 2): pluggable backend — `cpu` (Ultralytics,
  for laptop dev) and `hailo` (HailoRT on the AI HAT+, production on the Pi).

**Hardware decision: one tethered movable unit = Raspberry Pi 5 + AI HAT+ 13 TOPS
(Hailo-8L) + 2× wide CSI cameras**, in a 3D-printed case. Pi 5 from Mercado Livre;
AI HAT+ from MakerHero. The Hailo runs YOLOv8 person detection on both streams in
real time with huge headroom — enough to run a **larger/more accurate model**
(YOLOv8s/m) since only 2 streams share 13 TOPS. 4 GB RAM is fine (inference on the
Hailo). The HAT is on **PCIe**; the cameras are on **CSI** — so all USB ports stay
free and no hub is needed.

**Cameras: 2× wide CSI modules.** Recommended: **Raspberry Pi Camera Module 3
Wide** (IMX708, ~120° diagonal ≈ ~102° horizontal, autofocus). Two at ~90° apart
cover ~180°. ⚠️ Get the **Wide** variant — the standard Module 3 is only ~66°
horizontal and would leave a gap. Arducam wide IMX219 modules also work.

⚠️ **Pi 5 camera cable:** the Pi 5 uses the **narrow 22-pin** CSI connector, but
cameras ship with a **15-pin** ribbon — buy a **15→22-pin Pi 5 adapter cable** per
camera.

Detector backend = **HailoRT on the AI HAT+**:
- Officially supported on Pi OS via the **`hailo-all`** apt package (HailoRT +
  PCIe driver + tooling) — a maintained, smooth stack.
- Use a **pre-compiled YOLOv8/person `.hef`** from Hailo's model zoo (no
  self-compiling in the common case). The Hailo Dataflow Compiler is x86-only if
  you ever build a custom `.hef`.
- The HAT stacks above the Pi 5 (PCIe FPC + GPIO standoffs, above the active
  cooler) → the 3D case must allow the extra stack height.

### Future 360°

The 2 CSI ports cap the Pi 5 at 2 simultaneous cameras. To reach 360° (4 cameras)
later — **without a code rewrite** (the pipeline is already N-camera) — swap to a
**Compute Module 5 + 4-CSI carrier**, or **import USB camera modules** (Arducam
B0201 / ELP, UVC+MJPEG, ~90–120°, avoid fisheye) and add a powered hub. Design the
3D case with a **swappable front camera mount** so a 4-camera ring can replace the
2-camera front later.

Dev/test runs on a plain Windows/Linux laptop webcam (CPU detector) before the
parts arrive; the Hailo backend swaps in on the Pi via the pluggable detector
layer.

## Build phases

- [x] **Phase 1 — Scaffold & multi-camera capture.** Open N USB cameras, grab
      near-synchronized frame-sets, save to disk. Proves cameras + config work.
- [x] **Phase 2 — Detection + per-camera tracking.** YOLOv8 person detection
      (`cpu` Ultralytics for dev / `hailo` on the AI HAT+) + supervision ByteTrack,
      with annotated previews. See "Running Phase 2".
- [ ] **Phase 3 — Counting-geometry editor** (draw a tripwire line + dwell zone
      per camera, in image space; saved to config — quick to redo when re-aimed).
- [x] **Phase 4 — Counting logic.** Tripwire crossings = passers (footfall),
      zone dwell ≥ N s = stops with per-person **duration**; conversion = stops ÷
      passers. Events → `data/counts.sqlite`; `report.py` summarizes dwell stats.
      See "Running Phase 4".
- [ ] **Phase 5 — Output + LGPD** (counts to SQLite/CSV, rate-limited snapshots
      with auto-deletion / face blur).
- [ ] **Phase 6 — Service-ize** (systemd unit, runtime-reloadable config).

## Running Phase 1

```bash
# 1. Install deps (a venv is recommended)
python -m pip install -r requirements.txt

# 2. Edit config.yaml — set one camera to your laptop's webcam (source: 0)
#    On Windows, backend: dshow usually works best.

# 3. Capture
python capture.py
# Saves a timestamped frame-set every interval_seconds into data/snapshots/.
# Ctrl+C to stop.
```

## Running Phase 2 (detection + tracking)

Per camera: capture → detect → ByteTrack → annotated `data/annotated/<cam_id>.jpg`
(overwritten each cycle) + per-camera counts/FPS printed to the console.

**Laptop dev (USB webcam + CPU YOLOv8):**
```bash
python -m pip install -r requirements.txt   # incl. ultralytics + supervision
python detect_preview.py                    # uses config.yaml (cam0 = webcam, backend: cpu)
```

**On the Pi (2× CSI cameras + Hailo):**
```bash
sudo apt install -y python3-picamera2 hailo-all
ls /usr/share/hailo-models/                 # confirm yolov8s_h8l.hef path (edit config.pi.yaml if different)

python3 -m venv --system-site-packages .venv   # so it sees picamera2 + hailo
. .venv/bin/activate
pip install supervision PyYAML opencv-python    # NOT ultralytics — Hailo does detection

python detect_preview.py config.pi.yaml
```

View the annotated frames headlessly by pulling them (`scp pi-cam.local:.../data/annotated/cam0.jpg .`)
or serving the folder (`python -m http.server` in `data/annotated`).

> Known first-run tuning points: the exact `.hef` path, and the BGR/RGB channel
> order in `src/detect/hailo.py` (toggle the `cvtColor` if detections look weak).

## Running Phase 4 (counting)

Needs `geometry.yaml` (draw it with `tools/geometry_editor.html`; a placeholder
ships for plumbing tests).

```bash
python count.py config.pi.yaml     # live: passers / stops / conversion + SQLite log
python report.py                   # summary: passers, stops, conversion, dwell avg/median/p90/max
```
Annotated preview (tripwire + zone + HUD) lands in `data/annotated/<cam>.jpg`;
every PASS/STOP event is logged to `data/counts.sqlite`.

## LGPD note

Snapshots contain images of identifiable people → personal data under LGPD.
Phase 5 bakes in: opt-in + rate-limited snapshots, auto-deletion after N days,
optional face blur, and counts stored as anonymous aggregates. A posted notice
at the counter + a documented retention policy are part of the deployment.
