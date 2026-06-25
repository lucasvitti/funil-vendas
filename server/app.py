"""topofunil — counter-api (FastAPI + SQLite).

Receives count events from the board(s), serves aggregated reports (with
time-of-day buckets), and serves per-device config. Anonymous aggregates only —
no re-ID vectors here (those stay on-device until DPO sign-off).

Auth: every /api/* call needs  Authorization: Bearer <COUNTER_TOKEN>.
"""
from __future__ import annotations

import array
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

DB = os.environ.get("COUNTER_DB", "/data/counter.sqlite")
TOKEN = os.environ.get("COUNTER_TOKEN", "")              # admin token: full read/write + UI/downloads
INGEST = os.environ.get("COUNTER_INGEST_TOKEN", "")      # device token: upload + config-pull only (no reads)
SNAP_DIR = Path(os.environ.get("COUNTER_SNAP_DIR", "/data/snapshots"))
RETAIN_DAYS = int(os.environ.get("COUNTER_RETAIN_DAYS", "30"))
USER = os.environ.get("COUNTER_USER", "admin")
PASS = os.environ.get("COUNTER_PASS", "")

app = FastAPI(title="topofunil counter-api")

# devices with a pending on-demand "take shot" request from the editor
_CAPTURE_PENDING: set[str] = set()
_RESTART_AT: dict[str, float] = {}     # device -> last restart-request time; board polls + self-restarts
_LAST_SEEN: dict[str, float] = {}   # device -> server epoch of last board contact (heartbeat)


def _seen(device):
    if device:
        _LAST_SEEN[device] = time.time()


def _ensure_col(con, table, col, decl="TEXT"):
    # additive migration: add a column to an existing table if it isn't there yet
    if col not in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _img_date(f):
    """Event date for a snapshot: from its YYYYMMDD_ filename prefix, else file mtime."""
    n = f.name
    if len(n) >= 8 and n[:8].isdigit():
        return f"{n[0:4]}-{n[4:6]}-{n[6:8]}"
    return time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime))


def _img_time(f):
    """Time-of-day 'HH:MM' for a snapshot: from its _HHMMSS_ filename part, else mtime.
    Zero-padded so plain string comparison gives a correct time-of-day range filter."""
    n = f.name
    if len(n) >= 13 and n[:8].isdigit() and n[9:13].isdigit():
        return f"{n[9:11]}:{n[11:13]}"
    return time.strftime("%H:%M", time.localtime(f.stat().st_mtime))


def _images_per_bucket(device, gran, d_from, d_to, cam=None):
    """Count snapshot files (excluding _ref_) per time bucket, keyed like the SQL
    buckets ('YYYY-MM-DD HH:MM') via the filename timestamp (mtime fallback)."""
    out = {}
    d = SNAP_DIR / Path(device or "").name
    if not d.exists():
        return out
    for f in d.glob("*.jpg"):
        n = f.name
        if n.startswith("_"):
            continue
        if cam and f"_{cam}_" not in n:
            continue
        if len(n) >= 15 and n[:8].isdigit() and n[9:15].isdigit():
            date = f"{n[0:4]}-{n[4:6]}-{n[6:8]}"; hh, mm = n[9:11], int(n[11:13])
        else:
            t = time.localtime(f.stat().st_mtime)
            date = time.strftime("%Y-%m-%d", t); hh, mm = f"{t.tm_hour:02d}", t.tm_min
        if (d_from and date < d_from) or (d_to and date > d_to):
            continue
        out[f"{date} {hh}:{(mm // gran) * gran:02d}"] = out.get(f"{date} {hh}:{(mm // gran) * gran:02d}", 0) + 1
    return out


def _db():
    con = sqlite3.connect(DB)
    con.execute(
        "CREATE TABLE IF NOT EXISTS events("
        "device TEXT, ts TEXT, cam TEXT, type TEXT, track INTEGER, dwell REAL, place TEXT, "
        "direction TEXT, seg INTEGER, object TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS configs(device TEXT PRIMARY KEY, body TEXT, updated TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS vectors("
        "device TEXT, ts TEXT, cam TEXT, track INTEGER, event TEXT, mode TEXT, "
        "vec BLOB, clarity REAL, face_visible INTEGER, body_visible INTEGER, "
        "image TEXT, match_track INTEGER, match_dist REAL, received TEXT, place TEXT)"
    )
    _ensure_col(con, "events", "place")
    _ensure_col(con, "events", "direction")
    _ensure_col(con, "events", "seg", "INTEGER")
    _ensure_col(con, "events", "object")
    _ensure_col(con, "vectors", "place")
    return con


def _auth(authorization: str | None):
    # constant-time compare so a wrong token can't be guessed by response timing
    if not TOKEN or not authorization or not hmac.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(status_code=401, detail="unauthorized")


def _is_admin_bearer(authorization):
    return bool(TOKEN and authorization and hmac.compare_digest(authorization, f"Bearer {TOKEN}"))


def _is_ingest_bearer(authorization):
    return bool(INGEST and authorization and hmac.compare_digest(authorization, f"Bearer {INGEST}"))


def _auth_ingest(authorization):
    """Board-facing endpoints (upload + poll + config-pull): accept the restricted
    device/ingest token OR the admin token (a superset). Never a cookie — machine calls.
    The ingest token deliberately does NOT satisfy _auth_ui, so the board cannot read
    back events / vectors / images — only push them and pull its config."""
    if _is_ingest_bearer(authorization) or _is_admin_bearer(authorization):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def _auth_config(request, authorization=None, token_q=None):
    """Config READ: the board pulls it (ingest token); the editor loads it (cookie/admin)."""
    if _is_ingest_bearer(authorization):
        return
    _auth_ui(request, authorization, token_q)


def _auth_dl(authorization, token_q):
    # browser-friendly download auth: accept the Bearer header OR a ?token= query param
    ok = TOKEN and ((authorization and hmac.compare_digest(authorization, f"Bearer {TOKEN}"))
                    or (token_q and hmac.compare_digest(token_q, TOKEN)))
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized")


def _csv(rows, header, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _session_value():
    return hmac.new(TOKEN.encode(), b"counter-session", hashlib.sha256).hexdigest()


def _logged_in(request):
    c = request.cookies.get("session")
    return bool(TOKEN) and bool(c) and hmac.compare_digest(c, _session_value())


def _auth_ui(request, authorization=None, token_q=None):
    # web UI + downloads: accept the login cookie OR bearer header OR ?token=
    if _logged_in(request):
        return
    if TOKEN and ((authorization and hmac.compare_digest(authorization, f"Bearer {TOKEN}"))
                  or (token_q and hmac.compare_digest(token_q, TOKEN))):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def _agg(rows):
    """rows: (ts, type, dwell). Returns passers/stops/conversion/dwell stats."""
    passers = sum(1 for r in rows if r[1] == "pass")
    dwells = sorted(r[2] for r in rows if r[1] == "stop")
    stops = len(dwells)
    out = {
        "passers": passers,
        "stops": stops,
        "conversion_pct": round(stops / passers * 100, 1) if passers else 0.0,
    }
    if dwells:
        out["dwell_s"] = {
            "avg": round(sum(dwells) / len(dwells), 1),
            "median": round(dwells[len(dwells) // 2], 1),
            "max": round(dwells[-1], 1),
        }
    return out


@app.get("/health")
def health():
    return {"ok": True, "service": "topofunil counter-api"}


@app.post("/api/events")
async def post_events(request: Request, authorization: str = Header(None)):
    _auth_ingest(authorization)
    payload = await request.json()
    device = payload.get("device", "unknown")
    _seen(device)
    rows = payload.get("events", [])
    con = _db()
    con.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(device, e["ts"], e["cam"], e["type"], e.get("track"), e.get("dwell", 0.0),
          e.get("place", ""), e.get("direction", ""), e.get("seg", -1),
          e.get("object", "")) for e in rows],
    )
    con.commit()
    con.close()
    return {"inserted": len(rows)}


_GRAN_MIN = {10, 30, 60}


def _bucket_sql(gran):
    """SQLite expression flooring ts to a `gran`-minute slot -> 'YYYY-MM-DD HH:MM'.
    gran is validated against _GRAN_MIN before being inlined (no injection)."""
    if gran >= 60:
        return "strftime('%Y-%m-%d %H:00', ts)"
    return ("strftime('%Y-%m-%d %H:', ts) || "
            f"printf('%02d', (CAST(strftime('%M', ts) AS INTEGER)/{gran})*{gran})")


def _report_data(device=None, d_from=None, d_to=None, gran=60, place=None, obj=None, cam=None):
    """Aggregate conversion straight from the event rows, filtered by date range
    (and place) and bucketed to `gran` minutes. Date is part of each bucket."""
    gran = gran if gran in _GRAN_MIN else 60
    where, args = [], []
    if device:
        where.append("device = ?"); args.append(device)
    if place:
        where.append("place = ?"); args.append(place)
    if cam:
        where.append("cam = ?"); args.append(cam)
    if d_from:
        where.append("date(ts) >= ?"); args.append(d_from)
    if d_to:
        where.append("date(ts) <= ?"); args.append(d_to)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    # the conversion view only looks at pass/stop; raw 'cross' rows are CSV-only telemetry.
    # object lives only on events (not vectors), so it filters ev_* not the shared args.
    ev_where = where + ["type IN ('pass','stop')"]
    ev_args = list(args)
    if obj:
        ev_where.append("object = ?"); ev_args.append(obj)
    ev_wsql = " WHERE " + " AND ".join(ev_where)
    con = _db()
    tot = con.execute(
        "SELECT SUM(type='pass'), SUM(type='stop'), "
        "AVG(CASE WHEN type='stop' THEN dwell END), MAX(CASE WHEN type='stop' THEN dwell END), "
        "COUNT(*), MIN(ts), MAX(ts) FROM events" + ev_wsql, ev_args).fetchone()
    b = _bucket_sql(gran)
    brows = con.execute(
        f"SELECT {b} AS bkt, SUM(type='pass'), SUM(type='stop'), "
        f"AVG(CASE WHEN type='stop' THEN dwell END), GROUP_CONCAT(DISTINCT NULLIF(place,'')), "
        f"GROUP_CONCAT(DISTINCT NULLIF(object,'')), GROUP_CONCAT(DISTINCT NULLIF(cam,'')) "
        f"FROM events{ev_wsql} GROUP BY bkt ORDER BY bkt", ev_args).fetchall()
    # vectors saved per bucket — same WHERE works (vectors has device/place/ts too)
    vrows = con.execute(
        f"SELECT {b} AS bkt, COUNT(*) FROM vectors{wsql} GROUP BY bkt", args).fetchall()
    con.close()
    vecmap = {r[0]: r[1] for r in vrows}
    imgmap = _images_per_bucket(device, gran, d_from, d_to, cam)  # snapshot files by filename ts
    rows = []
    for bkt, p, s, dav, pl, obn, cmn in brows:
        p, s = p or 0, s or 0
        rows.append({"date": (bkt or "")[:10], "time": (bkt or "")[11:16],
                     "place": pl or "", "object": obn or "", "cam": cmn or "", "passers": p, "stops": s,
                     "conversion_pct": round(s / p * 100, 1) if p else 0.0,
                     "dwell_avg": round(dav, 1) if dav is not None else None,
                     "images": imgmap.get(bkt, 0), "vectors": vecmap.get(bkt, 0)})
    total_vectors = sum(r["vectors"] for r in rows)
    total_images = sum(r["images"] for r in rows)
    passers, stops, n = tot[0] or 0, tot[1] or 0, tot[4] or 0
    overall = {"passers": passers, "stops": stops,
               "conversion_pct": round(stops / passers * 100, 1) if passers else 0.0}
    if tot[2] is not None:
        overall["dwell_s"] = {"avg": round(tot[2], 1), "max": round(tot[3], 1)}
    return {
        "device": device or "all",
        "filter": {"from": d_from or None, "to": d_to or None, "gran": gran, "place": place or None, "object": obj or None, "cam": cam or None},
        "period": {"from": tot[5], "to": tot[6]} if n else None,
        "events": n,
        "vectors": total_vectors,
        "images": total_images,
        "overall": overall,
        "rows": rows,           # conversion + place + images/vectors per (date, time bucket)
    }


@app.get("/api/report")
def report(request: Request, device: str = None, authorization: str = Header(None),
           from_: str = Query(None, alias="from"), to: str = Query(None),
           gran: int = Query(60), place: str = Query(None), obj: str = Query(None, alias="object"),
           cam: str = Query(None), token: str = Query(None)):
    _auth_ui(request, authorization, token)          # report is a data read -> admin/cookie only
    return _report_data(device, from_, to, gran, place, obj, cam)


@app.get("/api/config/{device}")
def get_config(device: str, request: Request, authorization: str = Header(None), token: str = Query(None)):
    _auth_config(request, authorization, token)
    con = _db()
    row = con.execute("SELECT body FROM configs WHERE device = ?", (device,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404, detail="no config for device")
    return json.loads(row[0])


@app.put("/api/config/{device}")
async def put_config(device: str, request: Request, authorization: str = Header(None)):
    _auth_ui(request, authorization)
    body = await request.json()
    con = _db()
    con.execute(
        "INSERT INTO configs(device, body, updated) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(device) DO UPDATE SET body=excluded.body, updated=excluded.updated",
        (device, json.dumps(body)),
    )
    con.commit()
    con.close()
    return {"saved": device}


@app.post("/api/frame")
async def post_frame(request: Request, authorization: str = Header(None)):
    # board uploads a clean, face-pixelated reference frame per camera so the
    # hosted geometry editor has an up-to-date background to draw on.
    _auth_ingest(authorization)
    p = await request.json()
    device = p.get("device", "unknown")
    _seen(device)
    cam = Path(p.get("cam", "cam")).name
    img = p.get("image_b64")
    if not img:
        raise HTTPException(status_code=400, detail="image_b64 required")
    d = SNAP_DIR / device
    d.mkdir(parents=True, exist_ok=True)
    (d / f"_ref_{cam}.jpg").write_bytes(base64.b64decode(img))
    return {"saved": cam}


@app.get("/api/frame/{device}/{cam}.jpg")
def get_frame(device: str, cam: str, request: Request, authorization: str = Header(None), token: str = Query(None)):
    _auth_ui(request, authorization, token)
    f = SNAP_DIR / device / f"_ref_{Path(cam).name}.jpg"
    if not f.exists():
        raise HTTPException(status_code=404, detail="no reference frame yet")
    # never cache: "take shot" replaces this file in place and the editor must
    # always get the freshest bytes (the ?ts= query alone doesn't stop proxy caches).
    return Response(f.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.post("/api/capture/{device}")
def request_capture(device: str, request: Request, authorization: str = Header(None), token: str = Query(None)):
    # editor asks the board for an immediate fresh frame ("take shot")
    _auth_ui(request, authorization, token)
    _CAPTURE_PENDING.add(device)
    return {"requested": device}


@app.get("/api/capture/{device}")
def poll_capture(device: str, authorization: str = Header(None)):
    # board polls + consumes the flag (also our liveness heartbeat)
    _auth_ingest(authorization)
    _seen(device)
    pending = device in _CAPTURE_PENDING
    _CAPTURE_PENDING.discard(device)
    return {"pending": pending}


@app.post("/api/restart/{device}")
def request_restart(device: str, request: Request, authorization: str = Header(None), token: str = Query(None)):
    # editor/dashboard asks the board to restart so it re-pulls config + geometry
    _auth_ui(request, authorization, token)
    _RESTART_AT[device] = time.time()
    return {"requested": device, "at": _RESTART_AT[device]}


@app.get("/api/restart/{device}")
def poll_restart(device: str, authorization: str = Header(None)):
    # board polls this watermark; if it's newer than the board's startup baseline,
    # the board exits and systemd relaunches it (clean re-pull). Also a heartbeat.
    _auth_ingest(authorization)
    _seen(device)
    return {"at": _RESTART_AT.get(device, 0)}


@app.get("/api/frame_status/{device}")
def frame_status(device: str, request: Request, authorization: str = Header(None), token: str = Query(None)):
    # per-camera reference-frame mtime — lets the editor wait for a genuinely
    # fresh frame after "take shot" instead of guessing with fixed timers.
    _auth_ui(request, authorization, token)
    d = SNAP_DIR / device
    cams = {}
    if d.exists():
        for f in d.glob("_ref_*.jpg"):
            cams[f.name[len("_ref_"):-len(".jpg")]] = f.stat().st_mtime
    return {"cams": cams}


@app.post("/api/vectors")
async def post_vectors(request: Request, authorization: str = Header(None)):
    _auth_ingest(authorization)
    payload = await request.json()
    device = payload.get("device", "unknown")
    vs = payload.get("vectors", [])
    con = _db()
    con.executemany(
        "INSERT INTO vectors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?)",
        [(device, v["ts"], v["cam"], v.get("track"), v["event"], v["mode"],
          base64.b64decode(v["vec_b64"]), v.get("clarity"), v.get("face_visible"),
          v.get("body_visible"), v.get("image"), v.get("match_track"), v.get("match_dist"),
          v.get("place", "")) for v in vs],
    )
    # retention: drop vectors older than RETAIN_DAYS
    con.execute(f"DELETE FROM vectors WHERE received < datetime('now', '-{RETAIN_DAYS} days')")
    con.commit()
    con.close()
    return {"inserted": len(vs)}


@app.post("/api/snapshots")
async def post_snapshots(request: Request, authorization: str = Header(None)):
    _auth_ingest(authorization)
    payload = await request.json()
    device = payload.get("device", "unknown")
    name = payload.get("name")
    img_b64 = payload.get("image_b64")
    if not name or not img_b64:
        raise HTTPException(status_code=400, detail="name and image_b64 required")
    d = SNAP_DIR / device
    d.mkdir(parents=True, exist_ok=True)
    safe = Path(name).name  # strip any path components (no traversal)
    (d / safe).write_bytes(base64.b64decode(img_b64))
    # retention: delete snapshots older than RETAIN_DAYS
    cutoff = time.time() - RETAIN_DAYS * 86400
    for f in d.glob("*.jpg"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
    return {"saved": safe}


@app.get("/api/events.csv")
def events_csv(request: Request, device: str = None,
               from_: str = Query(None, alias="from"), to: str = Query(None),
               place: str = Query(None), obj: str = Query(None, alias="object"),
               cam: str = Query(None),
               authorization: str = Header(None), token: str = Query(None)):
    # honors the dashboard's active filters (range / place / object / camera) so the
    # download matches what's on screen.
    _auth_ui(request, authorization, token)
    where, args = [], []
    if device:
        where.append("device = ?"); args.append(device)
    if place:
        where.append("place = ?"); args.append(place)
    if obj:
        where.append("object = ?"); args.append(obj)
    if cam:
        where.append("cam = ?"); args.append(cam)
    if from_:
        where.append("date(ts) >= ?"); args.append(from_)
    if to:
        where.append("date(ts) <= ?"); args.append(to)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    con = _db()
    rows = con.execute(
        "SELECT device, place, object, ts, cam, type, track, dwell, direction, seg FROM events"
        + wsql + " ORDER BY ts", args).fetchall()
    con.close()
    return _csv(rows, ["device", "place", "object", "ts", "cam", "type", "track", "dwell", "direction", "seg"],
                "events.csv")


@app.get("/api/vectors.csv")
def vectors_csv(request: Request, device: str = None,
                from_: str = Query(None, alias="from"), to: str = Query(None),
                place: str = Query(None), cam: str = Query(None),
                fmt: str = Query("b64", alias="format"),
                authorization: str = Header(None), token: str = Query(None)):
    # vectors carry device/place/cam/ts (but no object column) — honor those filters.
    # format=numeric expands the float32 BLOB into v0..vN columns; default = base64 blob.
    _auth_ui(request, authorization, token)
    where, args = [], []
    if device:
        where.append("device = ?"); args.append(device)
    if place:
        where.append("place = ?"); args.append(place)
    if cam:
        where.append("cam = ?"); args.append(cam)
    if from_:
        where.append("date(ts) >= ?"); args.append(from_)
    if to:
        where.append("date(ts) <= ?"); args.append(to)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    con = _db()
    rows = con.execute(
        "SELECT device, place, ts, cam, track, event, mode, clarity, face_visible, "
        "body_visible, image, match_track, match_dist, vec FROM vectors" + wsql + " ORDER BY ts", args).fetchall()
    con.close()
    base_cols = ["device", "place", "ts", "cam", "track", "event", "mode", "clarity",
                 "face_visible", "body_visible", "image", "match_track", "match_dist"]
    if (fmt or "").lower() in ("numeric", "num", "expanded", "values"):
        # expand the float32 BLOB into v0..v{n-1} numeric columns (stdlib array, no numpy).
        # vdim derived from the data so it tracks the actual vector length (512 today).
        vdim = next((len(r[-1]) // 4 for r in rows if r[-1]), 512)
        header = base_cols + [f"v{i}" for i in range(vdim)]
        out = []
        for r in rows:
            blob = r[-1]
            if not blob:
                vals = [""] * vdim
            else:
                a = array.array("f")
                a.frombytes(blob[:vdim * 4])               # little-endian float32 (native on x86/ARM)
                vals = list(a) + [""] * (vdim - len(a))
            out.append(list(r[:-1]) + vals)
        return _csv(out, header, "vectors_numeric.csv")
    out = []
    for r in rows:
        r = list(r)
        r[-1] = base64.b64encode(r[-1]).decode() if r[-1] is not None else ""
        out.append(r)
    return _csv(out, base_cols + ["vec_b64"], "vectors.csv")


@app.post("/api/purge")
async def purge(request: Request, authorization: str = Header(None)):
    """LGPD erasure: delete re-ID vectors and/or face-pixelated snapshots for a
    device within a date range. dry_run=true returns the match counts without
    deleting (used by the UI to confirm before the real delete). Reference frames
    (_ref_*.jpg) are never touched; anonymous event counts are never deleted here."""
    _auth_ui(request, authorization)
    p = await request.json()
    device = Path(p.get("device", "pi-cam")).name
    d_from = p.get("from") or None
    d_to = p.get("to") or None
    dry = bool(p.get("dry_run", False))
    nvec = nimg = 0
    if p.get("vectors", True):
        where, args = ["device = ?"], [device]
        if d_from:
            where.append("date(ts) >= ?"); args.append(d_from)
        if d_to:
            where.append("date(ts) <= ?"); args.append(d_to)
        wsql = " WHERE " + " AND ".join(where)
        con = _db()
        nvec = con.execute("SELECT COUNT(*) FROM vectors" + wsql, args).fetchone()[0]
        if not dry:
            con.execute("DELETE FROM vectors" + wsql, args)
            con.commit()
        con.close()
    if p.get("images", True):
        d = SNAP_DIR / device
        if d.exists():
            for f in d.glob("*.jpg"):
                if f.name.startswith("_"):
                    continue  # editor reference frames — keep
                fd = _img_date(f)
                if (d_from and fd < d_from) or (d_to and fd > d_to):
                    continue
                nimg += 1
                if not dry:
                    f.unlink(missing_ok=True)
    return {"vectors": nvec, "images": nimg, "dry_run": dry}


# ---------------- web UI (login + dashboard + downloads) ----------------
# Brand mark: a clean metallic funnel ("topo de funil" = top of the funnel).
# Drop a PNG at /data/logo.png to override this with the real photo (no rebuild).
_FUNNEL_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" role="img" aria-label="topofunil funnel">
<defs>
<linearGradient id="a" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#fbfcfc"/><stop offset=".42" stop-color="#cbd0d5"/>
<stop offset=".52" stop-color="#eef0f2"/><stop offset="1" stop-color="#a6adb4"/></linearGradient>
<linearGradient id="b" x1="0" y1="0" x2="1" y2="0">
<stop offset="0" stop-color="#b3b9bf"/><stop offset=".5" stop-color="#f1f3f4"/>
<stop offset="1" stop-color="#a3aab1"/></linearGradient>
</defs>
<path d="M14 36 L55 88 L55 112 L73 112 L73 88 L114 36 Z" fill="url(#a)" stroke="#868d93" stroke-width="2.5" stroke-linejoin="round"/>
<ellipse cx="64" cy="36" rx="50" ry="14" fill="url(#b)" stroke="#868d93" stroke-width="2.5"/>
<ellipse cx="64" cy="35" rx="39" ry="9" fill="#dfe3e6" stroke="#9aa0a6" stroke-width="1.2"/>
</svg>"""


@app.get("/logo")
def logo():
    f = Path(DB).parent / "logo.png"   # drop a PNG here to use the real photo
    if f.exists():
        return Response(f.read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "max-age=300"})
    return Response(_FUNNEL_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "max-age=300"})


_LOGIN_HTML = """<!doctype html><meta charset=utf-8><title>topofunil login</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f4f7f8;color:#1a1a1a}
 .card{display:flex;width:92%;max-width:720px;background:#fff;border:1px solid #e5e8ea;border-radius:14px;overflow:hidden;box-shadow:0 8px 34px rgba(0,0,0,.07)}
 .left{flex:1;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#eef1f3,#dde2e5);padding:30px}
 .left img{width:220px;max-width:100%}
 .right{flex:1;padding:38px 34px;display:flex;flex-direction:column;justify-content:center}
 .right h2{margin:0;font-size:22px} .right .sub{margin:2px 0 20px;color:#8a8f93;font-size:13px}
 input{width:100%;padding:10px;margin:6px 0;box-sizing:border-box;border:1px solid #ccd1d4;border-radius:6px;font-size:14px}
 button{padding:10px 16px;margin-top:8px;background:#0a7;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:14px}
 #e{color:#c00;font-size:13px;min-height:16px;margin:8px 0 0}
 @media(max-width:560px){.card{flex-direction:column}.left{padding:22px}.left img{width:130px}}
</style>
<div class=card>
 <div class=left><img src="/logo" alt="topofunil"></div>
 <div class=right>
  <h2>topofunil</h2><div class=sub>top-of-funnel counter</div>
  <input id=u placeholder=user autofocus><input id=p type=password placeholder=password>
  <button onclick=go()>Login</button><p id=e></p>
 </div>
</div>
<script>
async function go(){
 const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({user:u.value,pass:p.value})});
 if(r.ok)location='/'; else document.getElementById('e').textContent='Invalid login';
}
document.addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script>"""


def _ago(secs):
    s = int(secs)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _freshness_html(device):
    """Liveness dot (from the board heartbeat) + latest event the VPS holds.
    The board polls /api/capture every ~0.75s, so 'last seen' tracks the live
    board even when there's no foot traffic to upload."""
    con = _db()
    last_ev = con.execute("SELECT MAX(ts) FROM events WHERE device=?", (device,)).fetchone()[0]
    con.close()
    seen = _LAST_SEEN.get(device)
    if seen is None:
        live = "<span style='color:#999'>&#9679; board: no heartbeat since server start</span>"
    else:
        ago = time.time() - seen
        # board contacts the VPS at least every ~60s (frame upload), faster if it
        # polls for captures — so <90s = healthy, climbing = trouble.
        color = "#0a7" if ago < 90 else ("#c80" if ago < 300 else "#c00")
        live = f"<span style='color:{color}'>&#9679; board last seen {_ago(ago)}</span>"
    return live, (last_ev or "&mdash;")


def _tabbar(active):
    """Shared top nav: Dashboard / Geometry / Gallery tabs + a fixed logout.
    `active` is one of 'dashboard'|'geometry'|'gallery' (the highlighted tab).
    Self-contained (ships its own <style>) so all three pages share one source."""
    def tab(key, href, label):
        on = " on" if key == active else ""
        return f'<a class="tab{on}" href="{href}">{label}</a>'
    return (
        "<style>"
        ".barin .lead{display:flex;align-items:center;gap:20px}"
        ".tabs{display:flex;gap:4px}"
        ".tabs .tab{padding:6px 12px;border-radius:6px;color:#555;font-size:14px;text-decoration:none}"
        ".tabs .tab:hover{background:#eef3f4}"
        ".tabs .tab.on{background:#0a7;color:#fff}"
        ".logout{color:#0a7;text-decoration:none;font-size:14px}"
        "</style>"
        "<div class=bar><div class=barin>"
        "<span class=lead>"
        "<span class=brand><img src='/logo' alt=''><b>topofunil</b></span>"
        "<nav class=tabs>"
        + tab("dashboard", "/", "Dashboard")
        + tab("geometry", "/config", "Geometry")
        + tab("gallery", "/gallery", "Gallery")
        + "</nav></span>"
        "<a class=logout href='/logout'>logout</a>"
        "</div></div>"
    )


_DASH_SORT_JS = """
(function(){
 const tb=document.getElementById('tb');
 const ths=Array.prototype.slice.call(document.querySelectorAll('thead th'));
 if(!tb||!ths.length)return;
 ths.forEach(function(th){th.dataset.label=th.textContent;th.style.cursor='pointer';th.style.userSelect='none';});
 const NUM=new Set([5,6,7,8,9]);          // passers/stops/conversion/images/vectors sort numerically
 let sc=0,sd='desc';                       // default: date+time, newest first
 function cv(tr,i){
  if(i===0)return tr.children[0].textContent.trim()+' '+(tr.children[1]?tr.children[1].textContent.trim():'');
  const t=(tr.children[i]?tr.children[i].textContent:'').trim();
  return NUM.has(i)?(parseFloat(t.replace('%',''))||0):t;
 }
 function drows(){return Array.prototype.slice.call(tb.querySelectorAll('tr')).filter(function(r){return r.children.length>1;});}
 function apply(){
  const rows=drows();
  if(rows.length){
   rows.sort(function(a,b){const av=cv(a,sc),bv=cv(b,sc);const c=av<bv?-1:(av>bv?1:0);return sd==='asc'?c:-c;});
   rows.forEach(function(r){tb.appendChild(r);});
  }
  ths.forEach(function(th,i){th.textContent=th.dataset.label+(i===sc?(sd==='asc'?' \\u25B2':' \\u25BC'):'');});
  pgi=0;pgRender();
 }
 ths.forEach(function(th,i){th.addEventListener('click',function(){
  if(i===sc)sd=(sd==='asc'?'desc':'asc');else{sc=i;sd=NUM.has(i)?'desc':'asc';}
  apply();
 });});
 apply();
})();
"""


def _dashboard_html(device, d_from=None, d_to=None, gran=60, place=None, obj=None, cam=None):
    if d_from is None and d_to is None:
        # fresh load with no date params -> default the filter to the current
        # (latest-data) day; the user can clear it for all-time or widen the range.
        con = _db()
        d_from = con.execute("SELECT MAX(date(ts)) FROM events WHERE device=?", (device,)).fetchone()[0]
        con.close()
    rep = _report_data(device, d_from, d_to, gran, place, obj, cam)
    o = rep["overall"]
    fl = rep["filter"]
    body = ""
    for a in reversed(rep.get("rows", [])):   # newest bucket first (JS can re-sort)
        body += (f"<tr><td>{a['date']}</td><td>{a['time']}</td><td>{_esc(a.get('place') or '—')}</td>"
                 f"<td>{_esc(a.get('object') or '—')}</td><td>{_esc(a.get('cam') or '—')}</td>"
                 f"<td>{a['passers']}</td><td>{a['stops']}</td><td>{a['conversion_pct']}%</td>"
                 f"<td>{a['images']}</td><td>{a['vectors']}</td></tr>")
    if not body:
        body = "<tr><td colspan=10 style='text-align:center;color:#999'>no events in this range</td></tr>"
    p = rep.get("period")
    per = f"{p['from']} &rarr; {p['to']}" if p else "&mdash;"
    nb = len(rep.get("rows", []))
    gsel = "".join(f"<option value={g}{' selected' if g == fl['gran'] else ''}>{g} min</option>"
                   for g in (10, 30, 60))
    con = _db()
    places = [r[0] for r in con.execute(
        "SELECT DISTINCT place FROM events WHERE device=? AND place IS NOT NULL AND place!='' "
        "ORDER BY place", (device,))]
    con.close()
    cur_place = fl.get("place") or ""
    psel = "<option value=''>all places</option>" + "".join(
        f'<option value="{_esc(pl)}"{" selected" if pl == cur_place else ""}>{_esc(pl)}</option>'
        for pl in places)
    con = _db()
    objs = [r[0] for r in con.execute(
        "SELECT DISTINCT object FROM events WHERE device=? AND object IS NOT NULL AND object!='' "
        "ORDER BY object", (device,))]
    con.close()
    cur_obj = fl.get("object") or ""
    osel = "<option value=''>all objects</option>" + "".join(
        f'<option value="{_esc(o2)}"{" selected" if o2 == cur_obj else ""}>{_esc(o2)}</option>'
        for o2 in objs)
    con = _db()
    cams = [r[0] for r in con.execute(
        "SELECT DISTINCT cam FROM events WHERE device=? AND cam IS NOT NULL AND cam!='' "
        "ORDER BY cam", (device,))]
    con.close()
    cur_cam = fl.get("cam") or ""
    csel = "<option value=''>all cameras</option>" + "".join(
        f'<option value="{_esc(c3)}"{" selected" if c3 == cur_cam else ""}>{_esc(c3)}</option>'
        for c3 in cams)
    vf, vt = fl["from"] or "", fl["to"] or ""
    live, latest = _freshness_html(device)
    qs = f"device={device}&amp;from={vf}&amp;to={vt}&amp;gran={fl['gran']}&amp;place={_esc(cur_place)}&amp;object={_esc(cur_obj)}&amp;cam={_esc(cur_cam)}"
    return f"""<!doctype html><meta charset=utf-8><title>topofunil</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{{font-family:system-ui;margin:0;color:#1a1a1a}}
 .bar{{position:sticky;top:0;z-index:50;background:#fff;border-bottom:1px solid #e5e8ea;box-shadow:0 1px 6px rgba(0,0,0,.04)}}
 .barin{{max-width:820px;margin:0 auto;padding:8px 16px;display:flex;justify-content:space-between;align-items:center}}
 .brand{{display:flex;align-items:center;gap:9px;font-size:16px}} .brand img{{height:28px}}
 .barin a{{color:#0a7;text-decoration:none;font-size:14px}}
 main{{max-width:820px;margin:0 auto;padding:18px 16px}}
 .fresh{{font-size:13px;color:#555;margin:0 0 6px}} .fresh a{{color:#0a7;text-decoration:none}}
 .k{{display:inline-block;margin-right:22px;font-size:14px;color:#555}}
 .k b{{display:block;font-size:24px;color:#111}}
 form.flt{{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:10px 0 4px;
  padding:10px 12px;background:#f7f9fa;border:1px solid #e5e8ea;border-radius:8px}}
 form.flt label{{font-size:12px;color:#555}}
 form.flt input,form.flt select{{display:block;margin-top:3px;padding:5px;border:1px solid #ccd1d4;border-radius:5px;font-size:14px}}
 form.flt button{{padding:7px 14px;background:#0a7;color:#fff;border:0;border-radius:5px;cursor:pointer;font-size:14px}}
 table{{border-collapse:collapse;margin-top:10px;width:100%;font-size:14px}}
 td,th{{border:1px solid #ddd;padding:5px 9px;text-align:right}} th{{background:#f4f4f4}}
 td:first-child,th:first-child,td:nth-child(2),th:nth-child(2),td:nth-child(3),th:nth-child(3),td:nth-child(4),th:nth-child(4),td:nth-child(5),th:nth-child(5){{text-align:left}}
 a.btn{{display:inline-block;margin:4px 8px 4px 0;padding:9px 13px;background:#0a7;color:#fff;
  text-decoration:none;border-radius:5px;font-size:14px}}
 .purge{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:6px;padding:10px 12px;
  background:#fff7f7;border:1px solid #f1d4d4;border-radius:8px;font-size:13px}}
 .purge label{{display:flex;align-items:center;gap:5px}}
 .purge input[type=text]{{padding:5px;border:1px solid #ccd1d4;border-radius:5px}}
 button.danger{{padding:7px 14px;background:#c0392b;color:#fff;border:0;border-radius:5px;cursor:pointer;font-size:14px}}
 #pmsg{{font-size:13px;color:#c0392b}}
 #pgnav{{display:none;gap:10px;align-items:center;margin:8px 0;font-size:14px}}
 #pgnav button{{padding:5px 11px;border:1px solid #ccd1d4;border-radius:5px;background:#fff;cursor:pointer}}
 #pgind{{color:#555}}
</style>
{_tabbar('dashboard')}
<main>
<div class=fresh>{live} &middot; latest event: {latest} &middot; <a href="?{qs}">&#8635; refresh</a> &middot; <a href="#" onclick="restartBoard();return false">&#8635; restart board</a></div>
<form class=flt method=get>
 <input type=hidden name=device value="{device}">
 <label>from<input type=text size=12 placeholder=yyyy-mm-dd pattern="\\d{{4}}-\\d{{2}}-\\d{{2}}" title="yyyy-mm-dd" name=from value="{vf}"></label>
 <label>to<input type=text size=12 placeholder=yyyy-mm-dd pattern="\\d{{4}}-\\d{{2}}-\\d{{2}}" title="yyyy-mm-dd" name=to value="{vt}"></label>
 <label>granularity<select name=gran>{gsel}</select></label>
 <label>place<select name=place>{psel}</select></label>
 <label>object<select name=object>{osel}</select></label>
 <label>camera<select name=cam>{csel}</select></label>
 <button type=submit>Apply</button>
</form>
<p style=color:#888;font-size:13px>range: {per} &middot; {rep['events']} events &middot; {rep['vectors']} vectors &middot; {rep['images']} images &middot; {nb} buckets</p>
<div>
 <span class=k>passers<b>{o['passers']}</b></span>
 <span class=k>stops<b>{o['stops']}</b></span>
 <span class=k>conversion<b>{o['conversion_pct']}%</b></span>
</div>
<h3>Downloads</h3>
<a class=btn href="/api/events.csv?{qs}">Report CSV</a>
<a class=btn href="/api/vectors.csv?{qs}">Vectors CSV</a>
<a class=btn href="/api/vectors.csv?{qs}&amp;format=numeric">Vectors (numeric)</a>
<a class=btn href="/api/snapshots.zip?{qs}">Images ZIP</a>
<h3>Conversion by {fl['gran']}-min bucket <span style="font-weight:400;font-size:12px;color:#999">— newest first; click a column to sort</span></h3>
<table><thead><tr><th>date</th><th>time</th><th>place</th><th>object</th><th>cam</th><th>passers</th><th>stops</th><th>conversion</th><th>images</th><th>vectors</th></tr></thead><tbody id=tb>{body}</tbody></table>
<div id=pgnav><button onclick=pgPrev()>&lsaquo; prev</button> <span id=pgind></span> <button onclick=pgNext()>next &rsaquo;</button></div>
<h3>Purge data (LGPD)</h3>
<p style="color:#888;font-size:12px;margin:2px 0">Permanently delete re-ID vectors and face-pixelated images in a date range (this device). Anonymous counts are kept. Empty dates = all.</p>
<div class=purge>
 <label>from <input type=text size=12 placeholder=yyyy-mm-dd pattern="\\d{{4}}-\\d{{2}}-\\d{{2}}" title="yyyy-mm-dd" id=pf></label>
 <label>to <input type=text size=12 placeholder=yyyy-mm-dd pattern="\\d{{4}}-\\d{{2}}-\\d{{2}}" title="yyyy-mm-dd" id=pt></label>
 <label><input type=checkbox id=pi checked> images</label>
 <label><input type=checkbox id=pv checked> vectors</label>
 <button class=danger onclick=purgeData()>Purge&hellip;</button>
 <span id=pmsg></span>
</div>
<script>
const PDEV={json.dumps(device)};
const PG=10; let pgi=0;
function pgRender(){{
 const rows=Array.prototype.slice.call(document.querySelectorAll('#tb tr'));
 const pages=Math.max(1,Math.ceil(rows.length/PG));
 if(pgi<0)pgi=0; if(pgi>pages-1)pgi=pages-1;
 rows.forEach((r,i)=>{{r.style.display=(i>=pgi*PG&&i<(pgi+1)*PG)?'':'none';}});
 document.getElementById('pgind').textContent='page '+(pgi+1)+' / '+pages;
 document.getElementById('pgnav').style.display=pages>1?'flex':'none';
}}
function pgPrev(){{pgi--;pgRender();}}
function pgNext(){{pgi++;pgRender();}}
{_DASH_SORT_JS}
async function restartBoard(){{
 if(!confirm('Restart the board now? It will re-pull config + geometry and resume counting in ~10–20s.'))return;
 const r=await fetch('/api/restart/'+encodeURIComponent(PDEV),{{method:'POST'}});
 alert(r.ok?'Restart requested — the board will pick it up within ~5s.':'Failed ('+r.status+')');
}}
async function purgeData(){{
 const m=document.getElementById('pmsg');
 const b={{device:PDEV,from:document.getElementById('pf').value||'',to:document.getElementById('pt').value||'',
  images:document.getElementById('pi').checked,vectors:document.getElementById('pv').checked}};
 if(!b.images&&!b.vectors){{m.textContent='pick images and/or vectors';return;}}
 m.textContent='checking…';
 const post=x=>fetch('/api/purge',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(x)}});
 let r=await post(Object.assign({{}},b,{{dry_run:true}}));
 if(!r.ok){{m.textContent='failed ('+r.status+')';return;}}
 const c=await r.json();
 if(c.vectors+c.images===0){{m.textContent='nothing matches that range';return;}}
 const rng=(b.from||'start')+' → '+(b.to||'now');
 if(!confirm('Permanently delete '+c.vectors+' vectors and '+c.images+' images ('+rng+')?\\nThis cannot be undone.')){{m.textContent='cancelled';return;}}
 m.textContent='purging…';
 r=await post(Object.assign({{}},b,{{dry_run:false}}));
 const res=await r.json();
 m.textContent='deleted '+res.vectors+' vectors, '+res.images+' images ✓';
}}
</script>
</main>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _LOGIN_HTML


@app.post("/login")
async def login(request: Request):
    b = await request.json()
    u = str(b.get("user", "")); pw = str(b.get("pass", ""))
    if PASS and hmac.compare_digest(u, USER) and hmac.compare_digest(pw, PASS):
        resp = JSONResponse({"ok": True})
        resp.set_cookie("session", _session_value(), httponly=True, secure=True,
                        samesite="lax", max_age=7 * 86400)
        return resp
    raise HTTPException(status_code=401, detail="invalid")


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session", httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, device: str = "pi-cam",
              from_: str = Query(None, alias="from"), to: str = Query(None),
              gran: int = Query(60), place: str = Query(None), obj: str = Query(None, alias="object"),
              cam: str = Query(None)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_dashboard_html(device, from_, to, gran, place, obj, cam))


_EDITOR_HTML = """<!doctype html><meta charset=utf-8><title>topofunil — geometry &amp; config</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui;margin:0;color:#1a1a1a}
 .bar{position:sticky;top:0;z-index:50;background:#fff;border-bottom:1px solid #e5e8ea;box-shadow:0 1px 6px rgba(0,0,0,.04)}
 .barin{max-width:1040px;margin:0 auto;padding:8px 16px;display:flex;justify-content:space-between;align-items:center}
 .brand{display:flex;align-items:center;gap:9px;font-size:16px} .brand img{height:28px}
 .barin a{color:#0a7;text-decoration:none;font-size:14px}
 main{max-width:1040px;margin:0 auto;padding:14px 16px}
 .cvwrap{margin-top:8px;max-height:74vh;overflow:auto;border:1px solid #888;border-radius:6px;background:#eee;resize:vertical}
 .cvwrap canvas{display:block;height:auto;max-width:none;cursor:crosshair}
 .cam{margin:14px 0;padding:12px;border:1px solid #e0e0e0;border-radius:8px}
 .cam h3{margin:0 0 8px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:6px}
 .caret{display:inline-block;color:#999;font-size:11px;transition:transform .15s}
 .cam.collapsed .caret{transform:rotate(-90deg)}
 .cam.collapsed .ctl,.cam.collapsed .cvwrap{display:none}
 .ctl{margin:6px 0;font-size:13px;color:#444;display:flex;flex-wrap:wrap;align-items:center;gap:4px}
 .zoom{display:flex;align-items:center;gap:6px;margin-left:auto;font-size:12px;color:#666}
 button{font-size:13px;padding:6px 10px;margin:2px 4px 2px 0;cursor:pointer;
  border:1px solid #bbb;border-radius:5px;background:#fafafa}
 button.pri{background:#0a7;color:#fff;border-color:#0a7;font-size:14px;padding:9px 16px}
 label.r{margin-right:10px} .tag{display:inline-block;padding:1px 6px;border-radius:4px;color:#fff;font-size:12px}
 .tline{background:#e6007a}.tzone{background:#00a0a0}
 fieldset{margin:14px 0;border:1px solid #e0e0e0;border-radius:8px}
 .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 18px}
 .grid label{font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:8px}
 .grid input,.grid select{padding:4px;width:130px}
 .pcat{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:#0a7;
  margin:16px 0 5px;border-bottom:1px solid #eee;padding-bottom:3px}
 .pcat:first-of-type{margin-top:2px}
 #msg{margin-left:10px;font-size:13px;color:#0a7}
 input#cams{padding:5px;width:200px}
 #hint{margin:10px 6px 2px;padding:8px 10px;background:#f4f7f7;border-left:3px solid #00a0a0;
  border-radius:4px;font-size:13px;color:#333;min-height:18px}
 .grid label:hover{background:#f7fbfb;border-radius:4px}
</style>
<!--TABBAR-->
<main>
<div class=ctl>cameras: <input id=cams value="cam0,cam1"> <button onclick=rebuild()>rebuild</button>
 <button onclick=loadCfg()>load current</button>
 <button id=shotbtn onclick=takeShot()>📷 take shot (all)</button>
 <span style=color:#888>rotate to upright first, then: tripwire = 2 clicks per segment (add one per approach) · zone = click each corner</span></div>
<div id=camwrap></div>

<fieldset><legend>parameters</legend>
<div class=pcat>Location</div>
<div style="margin:4px 0 8px;max-width:480px">
 <label style="font-size:13px;display:flex;align-items:center;gap:8px">Place / store name
  <input id=place_name type=text placeholder="e.g. Shopping Centro &mdash; Piso L1" style="flex:1;padding:5px;border:1px solid #ccd1d4;border-radius:5px"></label>
</div>
<div class=pcat>Detection &amp; counting</div>
<div class=grid>
 <label>Detection confidence (0-1) <input id=confidence type=number step=0.05 min=0 max=1></label>
 <label>Detect <select id=detect><option>person</option><option>cat</option><option>dog</option><option>banana</option><option>umbrella</option></select></label>
 <label>Dwell time for a "stop" (s) <input id=dwell_seconds type=number step=0.1></label>
 <label>Re-count cooldown (s) <input id=track_cooldown_s type=number step=0.1></label>
 <label>Crossing count <select id=cross_mode><option value=inward>inward (entries only)</option><option value=any>any direction</option></select></label>
 <label>Log every crossing <input id=log_crossings type=checkbox></label>
</div>
<div class=pcat>Privacy (LGPD)</div>
<div class=grid>
 <label>Face masking <select id=face_mode><option>pixelate</option><option>box</option><option>blur</option><option>off</option></select></label>
 <label>Keep snapshot with each vector <input id=store_images type=checkbox></label>
</div>
<div class=pcat>Re-identification</div>
<div class=grid>
 <label>Enable re-ID <input id=reid_enabled type=checkbox></label>
 <label>Re-ID type <select id=reid_mode><option>body</option><option>face</option><option>full</option></select></label>
 <label>Live match window (s) <input id=compare_window_s type=number step=1></label>
 <label>Vector retention (h) <input id=retention_hours type=number step=1></label>
 <label>Match threshold <input id=match_threshold type=number step=0.01></label>
</div>
<div class=pcat>Uploads to server</div>
<div class=grid>
 <label>Upload counts <input id=upload_events type=checkbox></label>
 <label>Upload re-ID vectors <input id=upload_vectors type=checkbox></label>
 <label>Upload snapshots <input id=upload_images type=checkbox></label>
</div>
<div class=pcat>Camera image (motion blur)</div>
<div class=grid>
 <label>Exposure / shutter µs (0=auto) <input id=exposure_us type=number step=500 min=0></label>
 <label>Gain / ISO (0=auto) <input id=gain type=number step=0.5 min=0></label>
</div>
<div class=pcat>Board capture timing</div>
<div class=grid>
 <label>Auto-shot interval (s) <input id=reference_frame_s type=number step=0.5 min=0.5></label>
 <label>"Take shot" poll interval (s) <input id=capture_poll_s type=number step=0.05 min=0.2></label>
</div>
<div id=hint>↳ hover or focus a field for a description</div></fieldset>

<button class=pri onclick=save()>Save to server</button><span id=msg></span>
<p style=color:#888;font-size:12px>The board applies this on its next start (pull-on-boot); local config.pi.yaml is the fallback.
Rotation is saved per camera and applied by the board at capture. Geometry coordinates are image pixels in the rotated (upright) view.</p>
</main>
<script>
const qs=new URLSearchParams(location.search), device=qs.get('device')||'pi-cam';
const msg=document.getElementById('msg');
let cams={};

function camList(){return document.getElementById('cams').value.split(',').map(s=>s.trim()).filter(Boolean);}

function rebuild(){
 const wrap=document.getElementById('camwrap'); const keep=cams; wrap.innerHTML=''; cams={};
 for(const id of camList()){
  const prev=keep[id];
  const div=document.createElement('div'); div.className='cam';
  const h=document.createElement('h3'); h.innerHTML='<span class=caret>▾</span> '+id; div.appendChild(h);
  const z0=prev?prev.zoom:100;
  const ctl=document.createElement('div'); ctl.className='ctl';
  ctl.innerHTML='<label class=r><input type=radio name=mode_'+id+' value=line checked> <span class="tag tline">tripwire</span></label>'+
   '<label class=r><input type=radio name=mode_'+id+' value=zone> <span class="tag tzone">zone</span> <span style="color:#777;font-size:11px">(counterclockwise)</span></label>'+
   '<button onclick="rot(\\''+id+'\\')">⟳ rotate</button>'+
   '<button onclick="undo(\\''+id+'\\')">undo</button><button onclick="clr(\\''+id+'\\')">clear</button>'+
   '<button onclick="upPick(\\''+id+'\\')">⬆ upload img</button>'+
   '<input type=file accept="image/*" id=file_'+id+' style="display:none" onchange="upFile(\\''+id+'\\',this)">'+
   '<span id=pts_'+id+'></span>'+
   '<span class=zoom>size <input type=range min=25 max=160 value='+z0+' oninput="zoom(\\''+id+'\\',this.value)"><span id=z_'+id+'>'+z0+'%</span></span>';
  div.appendChild(ctl);
  const cw=document.createElement('div'); cw.className='cvwrap';
  const cvs=document.createElement('canvas'); cvs.id='cv_'+id; cvs.width=960; cvs.height=540;
  cw.appendChild(cvs); div.appendChild(cw); wrap.appendChild(div);
  const st={lines:prev?prev.lines:[], pend:prev?prev.pend:[], zone:prev?prev.zone:[], rot:prev?prev.rot:0, zoom:z0, collapsed:prev?prev.collapsed:false, canvas:cvs, ctx:cvs.getContext('2d'), img:null};
  cams[id]=st;
  if(st.collapsed)div.classList.add('collapsed');
  h.onclick=()=>{st.collapsed=!st.collapsed; div.classList.toggle('collapsed',st.collapsed);};
  const im=new Image();
  im.onload=()=>{st.img=im; render(id);};
  im.onerror=()=>render(id);
  im.src='/api/frame/'+device+'/'+id+'.jpg?ts='+Date.now();
  cvs.onclick=ev=>{
   const r=cvs.getBoundingClientRect();
   const x=Math.round((ev.clientX-r.left)*(cvs.width/r.width));
   const y=Math.round((ev.clientY-r.top)*(cvs.height/r.height));
   const m=document.querySelector('input[name=mode_'+id+']:checked').value;
   if(m==='line'){st.pend.push([x,y]); if(st.pend.length===2){st.lines.push(st.pend); st.pend=[];}} else st.zone.push([x,y]);
   render(id);
  };
  render(id);
 }
}
function rot(id){const st=cams[id];
 // rotate the drawn points 90° CW into the swapped canvas so geometry follows the image
 const iw=st.img?st.img.naturalWidth:960, ih=st.img?st.img.naturalHeight:540;
 const H=(st.rot===90||st.rot===270)?iw:ih;   // old canvas height
 const xf=p=>[H-p[1],p[0]];
 st.lines=st.lines.map(s=>s.map(xf)); st.pend=st.pend.map(xf); st.zone=st.zone.map(xf);
 st.rot=(st.rot+90)%360; render(id);}
function undo(id){const st=cams[id];const m=document.querySelector('input[name=mode_'+id+']:checked').value;
 if(m==='line'){if(st.pend.length)st.pend.pop(); else st.lines.pop();} else st.zone.pop(); render(id);}
function clr(id){cams[id].lines=[];cams[id].pend=[];cams[id].zone=[];render(id);}
// display-only scale: canvas internal resolution stays at the original image
// size, so tripwire/zone coordinates are always in original pixels.
function zoom(id,v){const st=cams[id]; if(!st)return; st.zoom=+v; st.canvas.style.width=v+'%';
 const z=document.getElementById('z_'+id); if(z)z.textContent=v+'%';}
// upload a local image to draw geometry on (demo / when no live frame is available)
function upPick(id){const e=document.getElementById('file_'+id); if(e)e.click();}
function upFile(id,inp){const st=cams[id]; if(!st||!inp.files||!inp.files[0])return;
 const im=new Image(); im.onload=()=>{st.img=im; render(id);}; im.src=URL.createObjectURL(inp.files[0]);}

function reloadFrame(id){const st=cams[id]; if(!st)return; const im=new Image();
 im.onload=()=>{st.img=im; render(id);}; im.onerror=()=>render(id);
 im.src='/api/frame/'+device+'/'+id+'.jpg?ts='+Date.now();}

function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
async function frameStatus(){
 try{const r=await fetch('/api/frame_status/'+device,{cache:'no-store'});
  if(r.ok)return (await r.json()).cams||{};}catch(e){}
 return {};
}
// Ask the board for a fresh frame, then poll until each camera's frame actually
// changes and reload just that one — no fixed timers, no double reload.
async function takeShot(){
 const ids=Object.keys(cams); if(!ids.length){msg.textContent='add cameras first';return;}
 const btn=document.getElementById('shotbtn'); if(btn)btn.disabled=true;
 const before=await frameStatus();
 msg.textContent='requesting a fresh frame from the board…';
 try{await fetch('/api/capture/'+device,{method:'POST'});}catch(e){}
 const t0=Date.now(), deadline=15000, pending=new Set(ids);
 while(pending.size && Date.now()-t0<deadline){
  await sleep(500);
  const now=await frameStatus();
  for(const id of [...pending]){
   if(now[id] && now[id]>(before[id]||0)+0.001){reloadFrame(id); pending.delete(id);}
  }
  if(pending.size)msg.textContent='waiting for board… '+((Date.now()-t0)/1000|0)+'s';
 }
 msg.textContent = pending.size
   ? 'timed out — is the board running? ('+[...pending].join(', ')+')'
   : 'fresh frame loaded ✓';
 if(btn)btn.disabled=false;
}

function dot(ctx,p,c){ctx.fillStyle=c;ctx.beginPath();ctx.arc(p[0],p[1],6,0,7);ctx.fill();}
function arrow(ctx,x1,y1,x2,y2,c){ctx.strokeStyle=c;ctx.fillStyle=c;ctx.lineWidth=2;
 ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();
 const a=Math.atan2(y2-y1,x2-x1),h=9;
 ctx.beginPath();ctx.moveTo(x2,y2);ctx.lineTo(x2-h*Math.cos(a-0.5),y2-h*Math.sin(a-0.5));
 ctx.lineTo(x2-h*Math.cos(a+0.5),y2-h*Math.sin(a+0.5));ctx.closePath();ctx.fill();}
function render(id){
 const st=cams[id], ctx=st.ctx, cvs=st.canvas, rot=st.rot;
 const iw=st.img?st.img.naturalWidth:960, ih=st.img?st.img.naturalHeight:540;
 let cw=iw, ch=ih; if(rot===90||rot===270){cw=ih; ch=iw;}
 cvs.width=cw; cvs.height=ch;
 cvs.style.width=(st.zoom||100)+'%';   // display scale only — internal res = original pixels
 ctx.save();
 if(rot===90){ctx.translate(cw,0);ctx.rotate(Math.PI/2);}
 else if(rot===180){ctx.translate(cw,ch);ctx.rotate(Math.PI);}
 else if(rot===270){ctx.translate(0,ch);ctx.rotate(-Math.PI/2);}
 if(st.img){ctx.drawImage(st.img,0,0);} else {ctx.fillStyle='#ddd';ctx.fillRect(0,0,iw,ih);
  ctx.fillStyle='#999';ctx.font='16px system-ui';ctx.fillText('no reference frame for '+id+' — press “take shot”',20,30);}
 ctx.restore();
 if(st.zone.length){ctx.beginPath();ctx.moveTo(st.zone[0][0],st.zone[0][1]);st.zone.forEach(p=>ctx.lineTo(p[0],p[1]));
  ctx.closePath();ctx.fillStyle='rgba(0,160,160,0.25)';ctx.fill();ctx.strokeStyle='#00a0a0';ctx.lineWidth=3;ctx.stroke();
  st.zone.forEach(p=>dot(ctx,p,'#00a0a0'));}
 ctx.strokeStyle='#e6007a';ctx.lineWidth=4;
 st.lines.forEach(seg=>{ctx.beginPath();ctx.moveTo(seg[0][0],seg[0][1]);ctx.lineTo(seg[1][0],seg[1][1]);ctx.stroke();seg.forEach(p=>dot(ctx,p,'#e6007a'));});
 st.pend.forEach(p=>dot(ctx,p,'#e6007a'));
 if(st.zone.length>=3){const cx=st.zone.reduce((a,p)=>a+p[0],0)/st.zone.length,cy=st.zone.reduce((a,p)=>a+p[1],0)/st.zone.length;
  st.lines.forEach(seg=>{const mx=(seg[0][0]+seg[1][0])/2,my=(seg[0][1]+seg[1][1])/2;let dx=cx-mx,dy=cy-my;const L=Math.hypot(dx,dy)||1;arrow(ctx,mx,my,mx+dx/L*34,my+dy/L*34,'#e6007a');});}
 const el=document.getElementById('pts_'+id); if(el)el.textContent=' rot '+rot+'° · tripwires '+st.lines.length+(st.pend.length?' (+1 pending)':'')+' · zone '+st.zone.length+' pts';
}

const PARAMS=['place_name','dwell_seconds','track_cooldown_s','cross_mode','log_crossings','confidence','detect','face_mode','reid_enabled','reid_mode','compare_window_s','retention_hours','match_threshold','store_images','upload_events','upload_vectors','upload_images','capture_poll_s','reference_frame_s','exposure_us','gain'];
const BOOLS=['reid_enabled','store_images','upload_events','upload_vectors','upload_images','log_crossings'];
const NUMS=['dwell_seconds','track_cooldown_s','confidence','compare_window_s','retention_hours','match_threshold','capture_poll_s','reference_frame_s','exposure_us','gain'];
const HINTS={
 place_name:'Where this board is located (store/site name). Logged on every event, vector, and saved image; filterable on the dashboard. Set it before moving the kiosk.',
 dwell_seconds:'Seconds a person must stay inside the zone to count as a "stop" (dwell).',
 track_cooldown_s:'Minimum gap before the same tracked person can re-trigger a tripwire crossing.',
 cross_mode:'Which crossings count as a passer: "inward" = only entries toward the zone (cleaner funnel, stops ⊆ passers) · "any" = both directions. Direction is recorded on every counted crossing either way.',
 log_crossings:'Also log EVERY tripwire crossing as a "cross" event (direction in/out + segment) — full directional flow in the CSV, separate from the counted footfall. Off = only footfall + stops.',
 confidence:'Detector confidence threshold (0–1). Higher = fewer false detections but may miss people.',
 detect:'What the detector looks for (one COCO class). person/cat/dog for the usual tests, plus a few oddballs for fun. Everything is tracked/counted the same way.',
 face_mode:'How faces are obscured before any frame is saved or uploaded (LGPD): pixelate · box · blur · off.',
 reid_enabled:'Enable person re-identification (clothing/body vector). Pilot / study mode — no suppression.',
 reid_mode:'Re-ID feature: body = clothing (LGPD-safe) · face / full = biometric (gated).',
 compare_window_s:'Match a new vector only against vectors seen in the last N seconds.',
 retention_hours:'Auto-erase stored vectors older than this many hours.',
 match_threshold:'Cosine distance below which two vectors count as the same person (lower = stricter).',
 store_images:'Link each vector to its face-pixelated snapshot (never the original image).',
 upload_events:'Upload anonymous pass/stop counts to this server.',
 upload_vectors:'Upload re-ID vectors to the server (personal data — keep the DPO informed).',
 upload_images:'Upload face-pixelated event snapshots to the server.',
 capture_poll_s:'How often the board checks for a "take shot" request (seconds). Lower = snappier, more idle polling. Applies on next board start.',
 reference_frame_s:'How often the board auto-captures a fresh reference frame (seconds between shots). Lower = more frequent. Applies on next board start.',
 exposure_us:'Fixed shutter time in microseconds to freeze motion (0 = auto-exposure). Lower = sharper moving people but darker. ~8000≈1/125s, ~4000≈1/250s, ~2000≈1/500s. Raise gain to re-brighten. Manual exposure does NOT adapt to lighting changes. Applies on next board start.',
 gain:'Sensor gain (ISO) used when exposure is fixed (0 → a moderate default ≈4). Higher re-brightens a short exposure but adds grain. Try 4–8 indoors. Ignored when exposure = 0 (auto).'};
function wireHints(){const h=document.getElementById('hint'); const dflt=h.textContent;
 for(const k in HINTS){const el=document.getElementById(k); if(!el)continue;
  const lab=el.parentElement||el;
  el.title=HINTS[k]; lab.title=HINTS[k];
  const show=()=>{h.textContent=HINTS[k];}; const clear=()=>{h.textContent=dflt;};
  lab.addEventListener('mouseenter',show); lab.addEventListener('mouseleave',clear);
  el.addEventListener('focus',show); el.addEventListener('blur',clear);}}
// starting values when the server has nothing saved (mirror config.pi.yaml)
const DEFAULTS={place_name:'',dwell_seconds:1.8,track_cooldown_s:1.5,cross_mode:'inward',confidence:0.4,detect:'person',face_mode:'pixelate',
 reid_enabled:true,reid_mode:'body',compare_window_s:30,retention_hours:24,match_threshold:0.35,
 store_images:true,upload_events:true,upload_vectors:true,upload_images:true,capture_poll_s:0.75,reference_frame_s:10,
 log_crossings:true,exposure_us:0,gain:0};
function collectParams(){const p={};for(const k of PARAMS){const el=document.getElementById(k);if(!el)continue;
 if(BOOLS.includes(k))p[k]=el.checked; else if(NUMS.includes(k))p[k]=el.value===''?null:Number(el.value); else p[k]=el.value;}return p;}
function fillParams(p){if(!p)return;for(const k of PARAMS){const el=document.getElementById(k);if(!el||!(k in p))continue;
 if(BOOLS.includes(k))el.checked=!!p[k]; else el.value=p[k];}}

async function loadCfg(){
 fillParams(DEFAULTS);   // sensible starting point even before anything is saved
 let r; try{r=await fetch('/api/config/'+device,{cache:'no-store'});}catch(e){msg.textContent='load failed';return;}
 if(r.status===404){msg.textContent='nothing saved on server yet — showing defaults; draw geometry, adjust params, then Save';return;}
 if(!r.ok){msg.textContent='load failed ('+r.status+')';return;}
 const doc=await r.json();
 fillParams(doc.params||{});
 if(doc.geometry&&Object.keys(doc.geometry).length){
  document.getElementById('cams').value=Object.keys(doc.geometry).join(',');
  rebuild();
  for(const id in doc.geometry){const g=doc.geometry[id]; if(cams[id]){
   cams[id].rot=((g.rotate||0)%360+360)%360;
   const tw=g.tripwire||[]; const ls=(tw.length&&typeof tw[0][0]==='number')?[tw]:tw;
   cams[id].lines=ls.map(s=>s.map(p=>p.slice())); cams[id].pend=[];
   cams[id].zone=(g.zone||[]).slice(); render(id);}}
 }
 msg.textContent='loaded current config ✓';
}

async function save(){
 const geometry={};
 for(const id in cams){const st=cams[id];
  geometry[id]={rotate:st.rot};
  if(st.lines.length)geometry[id].tripwire=st.lines;
  if(st.zone.length>=3)geometry[id].zone=st.zone;}
 const doc={params:collectParams(),geometry:geometry};
 let r; try{r=await fetch('/api/config/'+device,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(doc)});}
 catch(e){msg.textContent='save failed';return;}
 msg.textContent=r.ok?'saved ✓ — board applies on its next start':'save failed ('+r.status+')';
}
wireHints();
rebuild();
loadCfg();
</script>"""


@app.get("/config", response_class=HTMLResponse)
def config_editor(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_EDITOR_HTML.replace("<!--TABBAR-->", _tabbar("geometry")))


@app.get("/api/snapshots.zip")
def snapshots_zip(request: Request, device: str = "pi-cam",
                  from_: str = Query(None, alias="from"), to: str = Query(None),
                  cam: str = Query(None),
                  authorization: str = Header(None), token: str = Query(None)):
    _auth_ui(request, authorization, token)
    d = SNAP_DIR / Path(device).name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if d.exists():
            for f in sorted(d.glob("*.jpg")):
                if f.name.startswith("_"):
                    continue  # skip reference frames (_ref_*.jpg)
                if cam and f"_{cam}_" not in f.name:
                    continue  # honor the camera filter
                fd = _img_date(f)
                if (from_ and fd < from_) or (to and fd > to):
                    continue  # honor the dashboard date range
                z.write(f, f.name)
    return Response(
        buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={device}_snapshots.zip"},
    )


@app.get("/api/snap/{device}/{name}")
def get_snap(device: str, name: str, request: Request,
             authorization: str = Header(None), token: str = Query(None)):
    """Serve one face-pixelated event snapshot for the gallery (cookie/bearer/token)."""
    _auth_ui(request, authorization, token)
    safe = Path(name).name                       # strip path components (no traversal)
    if not safe.lower().endswith(".jpg") or safe.startswith("_"):
        raise HTTPException(status_code=404, detail="not found")
    f = SNAP_DIR / Path(device).name / safe
    if not f.exists():
        raise HTTPException(status_code=404, detail="not found")
    return Response(f.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})


def _snap_caption(name):
    """Human label from a snapshot filename: 'HH:MM:SS · cam0 · pass' / 'dwell 2.1s'."""
    stem = name[:-4] if name.lower().endswith(".jpg") else name
    parts = stem.split("_")
    if len(parts) >= 5 and len(parts[0]) == 8 and parts[0].isdigit() and len(parts[1]) >= 6:
        t = f"{parts[1][0:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
        kind = parts[4]
        if kind == "dwell" and len(parts) >= 6:
            kind = f"dwell {parts[5]}"
        return f"{t} · {parts[3]} · {kind}"
    return name


def _gallery_html(device, d_from=None, d_to=None, page=0, t_from=None, t_to=None):
    dev = Path(device or "pi-cam").name
    d = SNAP_DIR / dev
    items = []
    if d.exists():
        for f in d.glob("*.jpg"):
            if not f.name.startswith("_"):
                items.append((f.name, _img_date(f), _img_time(f)))
    items.sort(key=lambda it: it[0], reverse=True)        # newest first (filename ts)
    dates = [fd for _, fd, _t in items]
    # default range = most recent day with images (robust to board/server clock skew)
    if not d_from and not d_to and dates:
        d_from = d_to = max(dates)
    # time filter is a time-of-day window applied within each day of the date range
    shown = [it for it in items
             if not ((d_from and it[1] < d_from) or (d_to and it[1] > d_to)
                     or (t_from and it[2] < t_from) or (t_to and it[2] > t_to))]
    total = len(shown)
    PER = 60
    pages = max(1, (total + PER - 1) // PER)
    page = max(0, min(int(page or 0), pages - 1))
    chunk = shown[page * PER:(page + 1) * PER]
    cells = ""
    for n, fd, _t in chunk:
        url = f"/api/snap/{dev}/{n}"
        cells += (f"<figure><a href='{url}' target=_blank><img loading=lazy src='{url}' alt=''></a>"
                  f"<figcaption>{_esc(fd[5:])} {_esc(_snap_caption(n))}</figcaption></figure>")
    if not cells:
        cells = "<p style='color:#999'>no images in this range</p>"
    vf, vt = d_from or "", d_to or ""
    vtf, vtt = t_from or "", t_to or ""
    base = f"device={dev}&amp;from={vf}&amp;to={vt}&amp;tfrom={vtf}&amp;tto={vtt}"
    prev_a = (f"<a class=pg href='?{base}&amp;page={page-1}'>&lsaquo; prev</a>"
              if page > 0 else "<span class='pg off'>&lsaquo; prev</span>")
    next_a = (f"<a class=pg href='?{base}&amp;page={page+1}'>next &rsaquo;</a>"
              if page < pages - 1 else "<span class='pg off'>next &rsaquo;</span>")
    pager = (f"<div class=pager>{prev_a} <span>page {page+1} / {pages} &middot; {total} images</span> {next_a}</div>"
             if total else "")
    return f"""<!doctype html><meta charset=utf-8><title>topofunil — gallery</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{{font-family:system-ui;margin:0;color:#1a1a1a}}
 .bar{{position:sticky;top:0;z-index:50;background:#fff;border-bottom:1px solid #e5e8ea;box-shadow:0 1px 6px rgba(0,0,0,.04)}}
 .barin{{max-width:1040px;margin:0 auto;padding:8px 16px;display:flex;justify-content:space-between;align-items:center}}
 .brand{{display:flex;align-items:center;gap:9px;font-size:16px}} .brand img{{height:28px}}
 main{{max-width:1040px;margin:0 auto;padding:18px 16px}}
 form.flt{{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:4px 0 14px;
  padding:10px 12px;background:#f7f9fa;border:1px solid #e5e8ea;border-radius:8px}}
 form.flt label{{font-size:12px;color:#555}}
 form.flt input{{display:block;margin-top:3px;padding:5px;border:1px solid #ccd1d4;border-radius:5px;font-size:14px}}
 form.flt button{{padding:7px 14px;background:#0a7;color:#fff;border:0;border-radius:5px;cursor:pointer;font-size:14px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}}
 figure{{margin:0}} figure img{{width:100%;height:128px;object-fit:cover;border-radius:6px;border:1px solid #ddd;background:#eee;display:block}}
 figcaption{{font-size:11px;color:#666;margin-top:3px}}
 .pager{{display:flex;gap:14px;align-items:center;justify-content:center;margin:18px 0;font-size:14px}}
 .pager .pg{{padding:6px 12px;border:1px solid #ccd1d4;border-radius:5px;color:#0a7;text-decoration:none}}
 .pager .pg.off{{color:#bbb;border-color:#eee}}
</style>
{_tabbar('gallery')}
<main>
<form class=flt method=get>
 <input type=hidden name=device value="{_esc(dev)}">
 <label>from date<input type=text size=12 placeholder=yyyy-mm-dd pattern="\\d{{4}}-\\d{{2}}-\\d{{2}}" title="yyyy-mm-dd" name=from value="{vf}"></label>
 <label>to date<input type=text size=12 placeholder=yyyy-mm-dd pattern="\\d{{4}}-\\d{{2}}-\\d{{2}}" title="yyyy-mm-dd" name=to value="{vt}"></label>
 <label>from time<input type=time name=tfrom value="{vtf}"></label>
 <label>to time<input type=time name=tto value="{vtt}"></label>
 <button type=submit>Apply</button>
</form>
<div class=grid>{cells}</div>
{pager}
</main>
"""


@app.get("/gallery", response_class=HTMLResponse)
def gallery(request: Request, device: str = "pi-cam",
            from_: str = Query(None, alias="from"), to: str = Query(None),
            tfrom: str = Query(None), tto: str = Query(None), page: int = Query(0)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_gallery_html(device, from_, to, page, tfrom, tto))
