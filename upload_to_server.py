"""Upload counts, re-ID vectors, and (face-pixelated) event images from the board
to the topofunil server. Per-stream state tracking so nothing is re-sent. Token from
COUNTER_TOKEN env or ~/.counter_token. Stdlib only.

    python upload_to_server.py [config.pi.yaml] [--loop]

server: block in config controls what uploads (events / vectors / images).
Images are deleted locally after a successful upload (delete_after_upload).
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from src.config import load_config
from src import server_config


def _token():
    t = os.environ.get("COUNTER_TOKEN")
    if t:
        return t.strip()
    f = Path.home() / ".counter_token"
    if f.exists():
        return f.read_text().strip()
    raise SystemExit("no token: set COUNTER_TOKEN or write ~/.counter_token")


def _post(url, obj, tok):
    body = json.dumps(obj).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def upload_events(base, device, tok, place=""):
    state = Path("data/.upload_state")
    last = int(state.read_text()) if state.exists() else 0
    con = sqlite3.connect("data/counts.sqlite")
    try:
        rows = con.execute(
            "SELECT rowid, ts, cam, type, track, dwell, direction, seg, object FROM events "
            "WHERE rowid > ? ORDER BY rowid", (last,)).fetchall()
    except sqlite3.OperationalError:          # new cols not migrated yet — pad defaults
        rows = [(*r, "", -1, "") for r in con.execute(
            "SELECT rowid, ts, cam, type, track, dwell FROM events WHERE rowid > ? ORDER BY rowid",
            (last,)).fetchall()]
    con.close()
    if not rows:
        return "events: 0"
    events = [{"ts": r[1], "cam": r[2], "type": r[3], "track": r[4], "dwell": r[5], "place": place,
               "direction": r[6], "seg": r[7], "object": r[8]} for r in rows]
    res = _post(base + "/api/events", {"device": device, "events": events}, tok)
    state.write_text(str(rows[-1][0]))
    return f"events: {res.get('inserted')}"


def upload_vectors(base, device, tok, compare_window, place=""):
    db = Path("data/reid.sqlite")
    if not db.exists():
        return "vectors: no-db"
    state = Path("data/.upload_vec_state")
    last = int(state.read_text()) if state.exists() else 0
    con = sqlite3.connect(str(db))
    # self-heal a stale watermark: if the local table was erased/reset (rowid
    # counter restarts at 1), `last` can sit above every live rowid and silently
    # suppress all uploads. Detect that and restart from 0.
    mx = con.execute("SELECT max(rowid) FROM vectors").fetchone()[0] or 0
    if mx < last:
        last = 0
    rows = con.execute(
        "SELECT rowid, ts, cam, track, event, mode, vec, clarity, face_visible, "
        "body_visible, image, match_track, match_dist FROM vectors WHERE rowid > ? ORDER BY rowid",
        (last,),
    ).fetchall()
    sent = 0
    if rows:
        vs = [
            {"ts": r[1], "cam": r[2], "track": r[3], "event": r[4], "mode": r[5],
             "vec_b64": base64.b64encode(r[6]).decode(), "clarity": r[7],
             "face_visible": r[8], "body_visible": r[9], "image": r[10],
             "match_track": r[11], "match_dist": r[12], "place": place}
            for r in rows
        ]
        res = _post(base + "/api/vectors", {"device": device, "vectors": vs}, tok)
        last = rows[-1][0]
        state.write_text(str(last))
        sent = res.get("inserted")
    # ERASE locally once uploaded AND past the live-matching window
    cutoff = time.time() - compare_window
    deleted = con.execute("DELETE FROM vectors WHERE rowid <= ? AND t < ?", (last, cutoff)).rowcount
    con.commit()
    con.close()
    return f"vectors: {sent} (erased {deleted})"


def upload_images(base, device, tok, delete=True):
    d = Path("data/events")
    if not d.exists():
        return "images: no-dir"
    files = sorted(d.glob("*.jpg"))
    sent = dropped = 0
    for f in files:
        try:
            data = f.read_bytes()
            if not data:                       # 0-byte/corrupt capture — junk it, don't wedge the queue
                f.unlink(missing_ok=True)
                dropped += 1
                continue
            b64 = base64.b64encode(data).decode()
            _post(base + "/api/snapshots", {"device": device, "name": f.name, "image_b64": b64}, tok)
            sent += 1
            if delete:
                f.unlink(missing_ok=True)
        except urllib.error.HTTPError as ex:
            # 4xx = the server rejected THIS file (empty/oversized/bad) — discard it and
            # keep going so one bad file can't block every later snapshot. 5xx is
            # transient (server-side): stop and retry the whole batch next cycle.
            if 400 <= ex.code < 500:
                print(f"  image {f.name} rejected ({ex.code}) — dropping")
                f.unlink(missing_ok=True)
                dropped += 1
                continue
            print(f"  image {f.name} failed: {ex} — retry next cycle")
            break
        except Exception as ex:
            print(f"  image {f.name} failed: {ex} — retry next cycle")
            break
    tail = f" (dropped {dropped})" if dropped else ""
    return f"images: {sent}{tail}"


def upload_frames(base, device, tok):
    """Push the per-camera reference frames (clean, face-pixelated) for the hosted
    geometry editor. Overwrites server-side; never deleted locally (kept refreshed)."""
    d = Path("data/frames")
    if not d.exists():
        return "frames: no-dir"
    sent = 0
    for f in sorted(d.glob("*.jpg")):
        try:
            b64 = base64.b64encode(f.read_bytes()).decode()
            _post(base + "/api/frame", {"device": device, "cam": f.stem, "image_b64": b64}, tok)
            sent += 1
        except Exception as ex:
            print(f"  frame {f.name} failed: {ex}")
            break
    return f"frames: {sent}"


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as ex:
        return f"{fn.__name__} ERROR: {ex}"


def main(config_path="config.pi.yaml", loop=False):
    cfg = load_config(config_path)
    s = cfg.get("server", {})
    if not s.get("enabled"):
        print("server upload disabled (server.enabled: false)")
        return 0
    base = s["url"].rstrip("/")
    device = s.get("device", "pi-cam")
    interval = float(s.get("upload_interval_s", 60))
    compare_window = float(cfg.get("reid", {}).get("compare_window_s", 30))
    tok = _token()

    # Resolve the board's location the same way count.py does on boot: the value set
    # in the hosted editor (server config) overrides the local config.pi.yaml name.
    place = cfg.get("location", {}).get("name", "")
    if s.get("pull_config", True):
        doc = server_config.fetch_config(base, device, tok)
        if doc:
            pv = (doc.get("params") or {}).get("place_name")
            if pv:
                place = pv
    if place:
        print(f"location: {place}")

    # remember the restart watermark at startup; if the operator bumps it from the
    # hosted UI, exit so systemd relaunches us (re-pulling place + server config).
    restart_baseline = server_config.check_restart(base, device, tok)
    while True:
        out = []
        if s.get("upload_events", True):
            out.append(_safe(upload_events, base, device, tok, place))
        if s.get("upload_vectors", False):
            out.append(_safe(upload_vectors, base, device, tok, compare_window, place))
        if s.get("upload_images", False):
            out.append(_safe(upload_images, base, device, tok, s.get("delete_after_upload", True)))
        if s.get("upload_frames", True):
            out.append(_safe(upload_frames, base, device, tok))
        print("  ".join(out))
        if server_config.check_restart(base, device, tok) > restart_baseline:
            print("restart requested from server — exiting for systemd relaunch", flush=True)
            sys.exit(0)
        if not loop:
            break
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    loop = "--loop" in args
    cfgp = next((a for a in args if not a.startswith("--")), "config.pi.yaml")
    sys.exit(main(cfgp, loop))
