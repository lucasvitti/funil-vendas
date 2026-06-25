# Counter Vision — Project & Web App Blueprint

A reusable design reference for a **two-tier, privacy-first camera-analytics system**: an
edge device that counts people (or any object) at a service point, and a hosted web app
("the hub") that configures the geometry, shows a conversion dashboard, and browses the
captured images. Written to be a *starting point* for a new project — the web-app patterns
(dashboard / geometry editor / gallery + an outbound-only edge feeding it) transfer to any
"device in the field → server with a config UI + reporting" system.

Reference implementation: `counter_vision` (edge) + `topofunil` counter-api (hub).

---

## Table of contents
1. [System at a glance](#1-system-at-a-glance)
2. [The hub web app (homepage)](#2-the-hub-web-app-homepage)
3. [The geometry editor](#3-the-geometry-editor)
4. [The gallery](#4-the-gallery)
5. [The edge pipeline](#5-the-edge-pipeline)
6. [Data model](#6-data-model)
7. [API surface](#7-api-surface)
8. [Privacy / LGPD](#8-privacy--lgpd)
9. [Deploy & ops](#9-deploy--ops)
10. [Reusable patterns (the takeaways)](#10-reusable-patterns-the-takeaways)

---

## 1. System at a glance

```
   EDGE (field device)                       HUB (VPS, public HTTPS)
 ┌──────────────────────┐   outbound only   ┌───────────────────────────┐
 │ camera(s)            │  HTTPS + Bearer   │ FastAPI + SQLite + nginx   │
 │  → detect (YOLO)     │ ────────────────▶ │  • ingest events/vectors/  │
 │  → track (ByteTrack) │   events,vectors, │    images/frames           │
 │  → count (lines+zone)│   images, frames  │  • serve config + geometry │
 │  → annotate + blur   │                   │  • web UI (3 tabs)          │
 │  → store + upload    │ ◀──── poll ─────  │  • command flags (poll)    │
 │  ← pull config/geom  │   capture/restart │                            │
 └──────────────────────┘                   └───────────────────────────┘
                                                  ▲ browser (cookie auth)
                                                  │ Dashboard · Geometry · Gallery
```

**Key decisions**
- **Edge is outbound-only.** The device never accepts inbound connections. The hub
  influences it through **poll flags** the device fetches (take-shot, restart) and a
  **config document** the device pulls on boot. This works behind any NAT/firewall.
- **Config + geometry live on the server** as a per-device JSON doc; the device pulls and
  applies it at startup. The field device ships with safe code defaults and needs no local
  editing.
- **The web UI is a single self-contained file per page** (inline CSS + vanilla JS, no
  build step, no CDN). Trivial to host, diff, and reason about.
- **Privacy first:** faces are pixelated on the device *before* anything is saved or sent.
- **One backend, two data classes:** anonymous aggregate counts (kept) vs. PII-ish
  artifacts (face-blurred images, appearance vectors) that are purgeable by date range.

Stack: Python 3, FastAPI, SQLite, nginx (Let's Encrypt + HSTS), Docker. Edge reference HW:
Raspberry Pi 5 + AI accelerator + CSI cameras, but the tier is swappable (any device that
can POST JSON works).

---

## 2. The hub web app (homepage)

A small FastAPI app that serves both the JSON API and the HTML UI. Every page shares a
**top tab bar**: `Dashboard · Geometry · Gallery`, with **logout pinned right**, active tab
highlighted. The bar is one shared helper so all pages stay in sync.

### Auth model
Three accepted credentials, checked with **constant-time compare** (`hmac.compare_digest`):
- **Session cookie** — set on `/login` (username+password), value = `HMAC(token, "session")`,
  flags `HttpOnly; Secure; SameSite=Lax`. Used by the browser UI.
- **Bearer token** — `Authorization: Bearer <TOKEN>`. Used by the edge device and API calls.
- **`?token=` query** — lets `<a href>` downloads and `<img src>` work from the browser.

Helpers: `_auth` (bearer only, for ingest), `_auth_ui` (cookie OR bearer OR token, for UI +
downloads). HTML pages redirect to `/login` when the cookie is missing. The token is never
logged or committed; it lives in an env file (`chmod 600`) on both ends.

### Dashboard (`/`)
A **conversion report** aggregated straight from event rows.

- **Time buckets:** events grouped into `10 / 30 / 60`-minute slots; each row is a
  `(date, time)` bucket. Conversion = `stops ÷ passers` per bucket (and overall).
- **Filters** (querystring-driven, all optional): `from` / `to` date, `granularity`,
  `place`, `object`, `camera`. Place/object/camera are dropdowns built from the distinct
  values present. **Dates are `yyyy-mm-dd` text inputs** (see *ISO date inputs* below).
- **Default view = the current (latest-data) day.** On a bare load with no date params,
  `from` defaults to `MAX(date(ts))` so the dashboard opens on "today" rather than all-time.
- **Table columns:** `date, time, place, object, cam, passers, stops, conversion,
  images, vectors`. The `cam` cell is `GROUP_CONCAT(DISTINCT cam)` per bucket (shows which
  camera(s) contributed).
- **Sorting:** rendered **newest-first** by default; **every column header is click-to-sort**
  (toggle asc/desc, `▲/▼` indicator). Date/place/object/cam sort lexically; numeric columns
  sort numerically. Sorting reorders the DOM rows and resets pagination.
- **Pagination:** 10 rows/page, prev/next, client-side.
- **Downloads** (all honor the *active filters* via the shared querystring):
  - **Report CSV** — `/api/events.csv` (one row per event: device, place, object, ts, cam,
    type, track, dwell, direction, seg).
  - **Vectors CSV** — `/api/vectors.csv`, appearance vector as base64 (`vec_b64`, compact).
  - **Vectors (numeric)** — `/api/vectors.csv?format=numeric`, the vector **expanded into
    `v0…vN` float columns** (stdlib, no numpy).
  - **Images ZIP** — `/api/snapshots.zip`, the face-blurred event snapshots.
- **Freshness indicator:** a live dot + "board last seen Ns ago" from the device's poll
  heartbeat (green `<90s`, amber `<300s`, red beyond), plus the latest event timestamp.
- **Restart board:** a link that bumps a server "restart" watermark the device polls; the
  device then self-exits and is relaunched by its supervisor, **re-pulling config + geometry**.
  (No inbound access to the device required — see the poll-flag pattern.)
- **Purge (LGPD):** delete appearance vectors and/or face-blurred images for a device within
  a date range. **Dry-run first** (returns match counts) → confirm → delete. Reference frames
  and anonymous event counts are never touched.

### Conventions used across the UI
- **Self-contained pages** — each HTML page is one string with an inline `<style>` and
  `<script>`; no external assets except `/logo` (overridable by dropping a PNG on the server).
- **ISO date inputs** — native `<input type=date>` renders in the *browser's locale*
  (`mm/dd/yyyy`, `dd/mm/yyyy`, …) and that can't be forced cross-browser. To guarantee
  `yyyy-mm-dd`, use a **text input** with `placeholder=yyyy-mm-dd` and
  `pattern="\d{4}-\d{2}-\d{2}"`. Trade-off: no native calendar popup (optionally re-add one
  with a button that calls `HTMLInputElement.showPicker()` and writes back ISO).
- **Shared filter querystring** — the same `device&from&to&gran&place&object&cam` string
  drives the view *and* every download link, so "what you see is what you download." Endpoints
  ignore params they don't declare.

---

## 3. The geometry editor (`/config`)

Draw the counting geometry on a live frame from each camera, tune parameters, and save —
the device applies it on its next start.

- **Per-camera canvas** drawn on a **reference frame** the device uploads (clean, already
  face-blurred). One collapsible panel per camera.
- **Multi-segment tripwire "fences":** a tripwire is a *list* of segments (2 clicks each).
  Multiple segments let you cover several approach directions to one zone (e.g. a corridor
  entered from both sides). Each segment renders an **inward arrow** toward the zone centroid
  so the entry direction is unambiguous.
- **Dwell zone:** a polygon (click each corner). A track that stays inside `≥ dwell_seconds`
  is a "stop".
- **Rotation:** a per-camera `rotate` (0/90/180/270). Rotating **also rotates the drawn
  geometry** so it stays put. The device captures upright using this value.
- **Upload image:** draw geometry on an uploaded still instead of the live frame (useful
  offline). **Take shot:** request a fresh frame on demand — sets a poll flag; the device
  pushes a new reference frame within its poll interval; the editor waits on the frame's
  mtime rather than a fixed timer.
- **Parameters** are grouped into **titled categories with friendly (non-code) labels**, each
  with a **hover hint** (shown on label *or* input hover). Examples: dwell seconds, detection
  classes, crossing mode (inward / any), "log every crossing", confidence, re-ID toggle +
  retention, upload toggles.
- **Save** → `PUT /api/config/{device}` stores `{params, geometry}`; the device pulls it on
  boot/restart. A message confirms "applies on next start".

**Geometry JSON shape**
```json
{
  "cam0": {
    "rotate": 270,
    "tripwire": [ [[x1,y1],[x2,y2]], [[x3,y3],[x4,y4]] ],   // list of segments
    "zone":     [ [x,y], [x,y], [x,y], [x,y] ]              // polygon
  },
  "cam1": { "rotate": 0, "tripwire": [...], "zone": [...] }
}
```

---

## 4. The gallery (`/gallery`)

Browse the captured event snapshots (face-blurred, with the analytics overlay burned in).

- **Filters:** `from`/`to` **date** range + a **time-of-day window** (`from time`/`to time`),
  applied within each day of the range (e.g. "08:00–09:00 across the week").
- **Default range** = the most recent day that has images (robust to clock skew between
  device and server).
- **Grid:** responsive, **lazy-loaded** thumbnails, newest first, paginated (≈60/page).
  Each caption is parsed from the filename: `time · cam · pass|dwell`.
- **Per-image serve:** `/api/snap/{device}/{name}` (auth, path-sanitized, reference frames
  excluded). Click a thumbnail → full size.

---

## 5. The edge pipeline

Per camera, every frame:

```
capture (rotated upright) → detect → track → count → annotate → store → (async) upload
```

- **Detect:** an object detector (YOLO family) restricted to **configurable classes**
  (person by default; the config accepts ids or names). Class id flows downstream so events
  can be filtered by object.
- **Track:** ByteTrack assigns stable track ids (and preserves class id).
- **Count (`CameraCounter`):** geometry-driven state machine.
  - **Tripwire crossing** via the sign of the cross-product (`side_of_line`); a sign flip
    between frames = a crossing. Per-segment **cooldown** kills jitter.
  - **Direction** is `in`/`out` relative to the zone centroid ("inward" = toward the zone).
  - **`cross_mode`**: `inward` (count only entries as passers) or `any`.
  - **`log_crossings`**: also emit a `cross` event for *every* crossing (full directional
    telemetry) so no field is ever null — `direction` is `in`/`out` for crossings, `stop`
    for dwells.
  - **Dwell zone** via ray-casting (`point_in_polygon`); enter→exit ≥ `dwell_seconds` = a
    **stop** with its dwell duration.
  - Conversion = `stops / passers`. (Note: in an *entrance* scenario this can exceed 100%
    and that is correct — `passers` counts only inward crossings, while `stops` includes
    people who enter from outside and dwell without an inward crossing.)
- **Annotate (overlay):** tripwire segments + inward arrows + translucent zone; per-track box
  labelled `class #id`; a HUD line `passers / stops / conv%`; the **place name** (gold) and
  the **camera id** (cyan, top-right). This annotated frame is what gets saved as the event
  snapshot.
- **Privacy:** `obscure_heads` pixelates the top ~28% of each **person** box (HSV mosaic)
  *before* drawing/saving. Scoped to the person class only.
- **Appearance vector (re-ID):** on each pass/stop, extract a vector for the person crop and
  record it with quality metadata; find the nearest recent vector (cosine). See §7-reID.
- **Storage:** events DB (SQLite), re-ID DB (vectors), event snapshots (`<ts>_<cam>_<type>.jpg`),
  per-camera reference frames (`_ref_<cam>.jpg`).
- **Upload loop** (separate process): pushes new events, vectors (base64), images, and frames
  on an interval. **Resilient:** skips/drops 0-byte or server-rejected (4xx) files instead of
  wedging the queue; a stale upload watermark self-heals if the local table was reset.
- **Commands:** the main loop polls `capture` (take-shot) and `restart` flags; on restart it
  commits and exits so the supervisor relaunches it → config/geometry re-pull.

Runs as **supervised services** (systemd user units with `Restart=always`,
`KillSignal=SIGINT`) so "restart" is just a clean exit.

---

## 6. Data model

**SQLite tables (hub):**
```
events(device, ts, cam, type, track, dwell, place, direction, seg, object)
    type ∈ {pass, cross, stop};  direction ∈ {in, out, stop};  seg = tripwire index
vectors(device, ts, cam, track, event, mode, vec BLOB, clarity,
        face_visible, body_visible, image, match_track, match_dist, received, place)
configs(device, body, updated)          -- body = JSON {params, geometry}
```
Columns are added with idempotent `ALTER TABLE … ADD COLUMN` guards so schema evolves
without migrations.

**Files:** `snapshots/<device>/<ts>_<cam>_<pass|dwell>.jpg` (events) and `_ref_<cam>.jpg`
(editor reference frames).

**`vec` is a raw binary BLOB** — `float32` bytes, little-endian, L2-normalised. Base64 only
appears at the **text boundaries** (the JSON upload `vec_b64` and the CSV export); the
database stores raw bytes. Decode with `np.frombuffer(blob, "<f4")` (or stdlib `array('f')`).

**Config params** (server → device, applied on boot): place name, dwell seconds, track
cooldown, cross mode, log-crossings, detection classes, confidence, face mode, re-ID
enable/mode/window/retention/threshold, store-images, upload toggles, poll/reference-frame
intervals. A `_PARAM_MAP` maps each flat param to its `(section, key)` in the device config.

---

## 7. API surface

**Ingest (Bearer only):**
```
POST /api/events            POST /api/vectors        POST /api/snapshots     POST /api/frame
GET  /api/capture/{device}  GET  /api/restart/{device}      ← device polls (+ heartbeat)
GET  /api/config/{device}                                    ← device pulls on boot
```
**UI + downloads (cookie / Bearer / ?token):**
```
GET  /            (dashboard)     GET /config (editor)     GET /gallery
GET  /api/report                 (JSON, same aggregation the dashboard renders)
GET  /api/events.csv             /api/vectors.csv[?format=numeric]   /api/snapshots.zip
GET  /api/snap/{device}/{name}   /api/frame/{device}/{cam}.jpg       /api/frame_status/{device}
POST /api/capture/{device}       POST /api/restart/{device}          POST /api/purge
PUT  /api/config/{device}
GET  /login  POST /login  GET /logout  GET /health  GET /logo
```
All report/CSV endpoints accept the same filter params (`device,from,to,gran,place,object,cam`)
so downloads mirror the dashboard. Endpoints ignore params they don't declare, so one shared
querystring is safe to pass everywhere.

**re-ID detail:** the appearance vector is an **HSV colour histogram** (8×8×8 = **512-dim
float32**, L2-normalised), compared by **cosine distance** (`1 − a·b`, both unit-norm). It is
a deliberate *placeholder* baseline — ephemeral, pilot-mode, gated behind a DPO review for
any wider use. Upgrade path: striped histogram + LBP (LOMO), or an OSNet embedding.

---

## 8. Privacy / LGPD

- **Faces pixelated on the device** before any frame is saved or uploaded (person class only).
- **Two data classes:** anonymous aggregate counts (retained) vs. face-blurred images +
  appearance vectors (PII-adjacent). The latter are **purgeable by date range** from the UI
  (dry-run + confirm), and auto-expire by retention.
- **Secrets:** the API token is never printed or committed (env file, `chmod 600`, both ends).
  Cookies are `Secure; HttpOnly`; nginx enforces HTTPS + HSTS; auth uses constant-time compare.
- **Cookieless analytics** for the public site (if any) to avoid consent banners.

---

## 9. Deploy & ops

- **Server:** the app file is **baked into the Docker image** (`COPY app.py`), only `./data`
  is bind-mounted. Deploy = `scp app.py` then **`docker compose up -d --build`** — *not*
  `restart`, which silently runs the old image. **Back up the live file first** (timestamped),
  then verify the change actually shipped (don't trust the health check alone).
- **Inspecting server data:** files live in the `./data` bind mount → look on the host at
  `…/data/...` or `docker exec <container> ls /data/...`, **not** host-absolute `/data`.
- **Edge:** `scp` the code + restart the supervised services (or click *Restart board*).
  Output buffering can hide `print()` from the journal — **verify by behavior** (poll
  heartbeat on the server, fresh annotated frames) rather than logs.
- **Smoke-testing templates without logging in:** render server-side in the container —
  `docker exec … python -c "import app; app._dashboard_html('dev')"` — to catch template
  errors; pair with HTTP checks on the JSON/CSV endpoints using the bearer token.

---

## 10. Reusable patterns (the takeaways)

1. **Outbound-only edge + poll-flag command channel.** The field device never listens; the
   server raises flags (take-shot, restart) the device fetches on a short poll, which doubles
   as a **liveness heartbeat**. Works behind any NAT.
2. **Server-stored config + geometry as one JSON doc, pulled on boot.** Field devices stay
   dumb; all tuning happens in the hosted editor. A flat `param → (section, key)` map keeps a
   friendly UI decoupled from the device's config shape.
3. **Self-contained single-file pages.** Inline CSS/JS, no build, no CDN — a shared tab-bar
   helper and a couple of small JS blocks (sort, pagination) are injected as plain strings to
   sidestep f-string brace-escaping.
4. **One filter querystring for view *and* downloads.** Build it once; every table, CSV, and
   ZIP link consumes the same params. "What you see is what you download."
5. **ISO `yyyy-mm-dd` text date inputs.** Native date pickers obey the browser locale and
   can't be forced; a `pattern`-validated text field guarantees the format everywhere.
6. **Dry-run-then-confirm for destructive actions** (purge), and **never delete the things
   you didn't create** (reference frames, anonymous counts) when erasing PII.
7. **Resilient upload queue:** drop poison files (0-byte / 4xx) and continue; self-heal a
   stale "already-sent" watermark when the source table resets — one bad row must never wedge
   the whole pipeline.
8. **Privacy-first capture:** irreversible blurring happens *before* the first write, scoped
   to the sensitive class only.
9. **Verify by behavior, deploy by rebuild.** Buffered logs lie; baked images go stale on
   `restart`. Render templates server-side and rebuild on deploy.
10. **Geometry that travels with its frame.** Tripwires/zones are stored in the upright
    frame's coordinates; rotation transforms the geometry too, so the drawing never drifts.

---

*Generated as a starting-point reference. Hardware specifics (Pi 5 + accelerator + CSI
cameras) are one realization of the "edge tier" — any device that can POST JSON over HTTPS
slots into the same hub design.*
