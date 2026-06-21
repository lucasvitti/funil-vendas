"""Phase 4: live counting — passers (tripwire) + stops & dwell time (zone).

    python count.py config.pi.yaml

Per camera: capture -> detect -> ByteTrack -> tripwire/zone counting. Logs each
pass/stop event to data/counts.sqlite, draws an annotated preview to
data/annotated/<cam>.jpg, and prints running passers / stops / conversion.
Ctrl+C to stop. Use report.py to summarize dwell-time stats.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import supervision as sv

from src.cameras import build_camera
from src.config import load_config
from src.counting.counter import CameraCounter
from src.counting.geometry import load_geometry
from src.detect import build_detector, to_sv
from src.privacy import obscure_heads
from src import reid, server_config

LINE_COLOR = (140, 0, 212)   # BGR magenta
ZONE_COLOR = (160, 150, 10)  # BGR teal

# Inverse of the camera's upright rotation — used to turn an upright (rotated)
# frame back into the raw sensor orientation before uploading it as the editor
# reference frame, so the hosted editor owns rotation (raw in, rotate in-browser).
_INV_ROT = {
    90: cv2.ROTATE_90_COUNTERCLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_CLOCKWISE,
}


def unrotate(img, rotate):
    r = int(rotate) % 360
    return cv2.rotate(img, _INV_ROT[r]) if r in _INV_ROT else img


def foot_point(xyxy):
    x1, _, x2, y2 = xyxy
    return ((x1 + x2) / 2.0, float(y2))


def draw(image, geom, tracked, counter, privacy, place=""):
    img = image.copy()
    # Privacy first: obscure faces on the raw content before anything is drawn,
    # so the saved frame never contains an identifiable face (LGPD).
    obscure_heads(img, tracked.xyxy, **privacy)
    if geom.get("zone"):
        pts = np.array(geom["zone"], dtype=int).reshape(-1, 1, 2)
        overlay = img.copy()
        cv2.fillPoly(overlay, [pts], ZONE_COLOR)
        img = cv2.addWeighted(overlay, 0.22, img, 0.78, 0)
        cv2.polylines(img, [pts], True, ZONE_COLOR, 2)
    if geom.get("tripwire"):
        a, b = geom["tripwire"]
        cv2.line(img, tuple(map(int, a)), tuple(map(int, b)), LINE_COLOR, 3)
    for box, tid in zip(tracked.xyxy, tracked.tracker_id):
        x1, y1, x2, y2 = map(int, box)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.circle(img, ((x1 + x2) // 2, y2), 5, (0, 140, 255), -1)  # foot point
        cv2.putText(img, f"#{int(tid)}", (x1, max(0, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    hud = f"passers {counter.passers}  stops {counter.stops}  conv {counter.conversion * 100:.0f}%"
    cv2.putText(img, hud, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, hud, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 1, cv2.LINE_AA)
    if place:
        cv2.putText(img, place, (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, place, (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 215, 255), 1, cv2.LINE_AA)
    return img


def _stamp(t):
    return t.strftime("%Y%m%d_%H%M%S_") + f"{t.microsecond // 1000:03d}"


def save_event_snapshot(events_dir, e, image, max_files):
    """Write a named, already-anonymized snapshot for one event, then prune the
    oldest beyond max_files so the SD can never fill up.
        pass -> pass_<cam>_<timestamp>.jpg
        stop -> <cam>_dwell_<begin-timestamp>_<dwell>s.jpg
    """
    now = datetime.now()
    if e.type == "pass":
        name = f"{_stamp(now)}_{e.cam_id}_pass"
    else:  # stop / dwell
        begin = now - timedelta(seconds=e.dwell)
        name = f"{_stamp(begin)}_{e.cam_id}_dwell_{e.dwell:.1f}s"
    cv2.imwrite(str(events_dir / f"{name}.jpg"), image)
    if max_files > 0:
        files = sorted(events_dir.glob("*.jpg"))
        for old in files[:-max_files]:
            old.unlink(missing_ok=True)
    return f"{name}.jpg"


def main(config_path: str | None = None) -> int:
    cfg = load_config(config_path) if config_path else load_config()

    # Pull per-device config (tunable params + geometry) from the server. Local
    # config.pi.yaml / geometry.yaml are the fallback on any failure.
    scfg = cfg.get("server", {})
    server_geom = None
    if scfg.get("enabled") and scfg.get("pull_config", True):
        doc = server_config.fetch_config(scfg.get("url"), scfg.get("device", "pi-cam"))
        server_geom = server_config.apply_config(cfg, doc)

    ccfg = cfg.get("counting", {})
    geometry = server_geom if server_geom else load_geometry(ccfg.get("geometry_file", "geometry.yaml"))
    dwell_s = float(ccfg.get("dwell_seconds", 3.0))
    cooldown = float(ccfg.get("track_cooldown_s", 2.0))

    pcfg = cfg.get("privacy", {})
    privacy = {
        "mode": pcfg.get("face_mode", "pixelate"),
        "head_frac": float(pcfg.get("head_fraction", 0.28)),
        "blocks": int(pcfg.get("pixelate_blocks", 12)),
    }
    print(f"privacy: face_mode={privacy['mode']}")

    place = cfg.get("location", {}).get("name", "")   # stamped on images; logged via upload
    if place:
        print(f"location: {place}")

    detector = build_detector(cfg.get("detection", {}))
    cameras = [build_camera(c) for c in cfg["cameras"]]
    for cam in cameras:
        cam.open()
    trackers = {c.cam_id: sv.ByteTrack() for c in cameras}
    counters = {
        c.cam_id: CameraCounter(c.cam_id, geometry.get(c.cam_id, {}), dwell_s, cooldown)
        for c in cameras
    }
    for c in cameras:
        if not geometry.get(c.cam_id):
            print(f"  [warn] no geometry for {c.cam_id} — draw it with the editor + geometry.yaml")

    Path("data").mkdir(exist_ok=True)
    db = sqlite3.connect("data/counts.sqlite")
    db.execute(
        "CREATE TABLE IF NOT EXISTS events "
        "(ts TEXT, cam TEXT, type TEXT, track INTEGER, dwell REAL)"
    )
    db.commit()
    ann = Path("data/annotated")
    ann.mkdir(parents=True, exist_ok=True)

    ecfg = cfg.get("events", {})
    snap_mode = ecfg.get("snapshot", "both")          # off | pass | stop | both
    snap_max = int(ecfg.get("max_files", 400))
    events_dir = Path(ecfg.get("dir", "data/events"))
    if snap_mode != "off":
        events_dir.mkdir(parents=True, exist_ok=True)
        print(f"event snapshots: {snap_mode} -> {events_dir} (keep newest {snap_max})")

    # Reference frames for the hosted geometry editor: a clean, face-pixelated,
    # RAW (un-rotated) frame per camera, refreshed every reference_frame_s and
    # uploaded by upload_to_server.py. The editor owns rotation. An on-demand
    # "take shot" from the editor is polled here and uploaded immediately.
    frames_dir = Path("data/frames")
    write_frames = bool(scfg.get("enabled") and scfg.get("upload_frames", True))
    ref_interval = float(scfg.get("reference_frame_s", 10))
    srv_base = scfg.get("url")
    srv_device = scfg.get("device", "pi-cam")
    srv_token = server_config._token() if scfg.get("enabled") else None
    cap_check_s = float(scfg.get("capture_poll_s", 0.75))
    last_ref: dict[str, float] = {}
    last_cap_check = 0.0
    if write_frames:
        frames_dir.mkdir(parents=True, exist_ok=True)
        print(f"reference frames: every {ref_interval:.0f}s (raw) -> {frames_dir}")

    rcfg = cfg.get("reid", {})
    reid_store = None
    reid_mode = rcfg.get("mode", "body")
    reid_window = float(rcfg.get("compare_window_s", 30))
    reid_keep_img = bool(rcfg.get("store_images", True))
    last_prune = 0.0
    if rcfg.get("enabled", False):
        reid_store = reid.ReidStore(rcfg.get("db", "data/reid.sqlite"),
                                    retention_hours=float(rcfg.get("retention_hours", 24)))
        print(f"re-id: mode={reid_mode}, compare_window={reid_window}s (study mode — no suppression)")

    time.sleep(float(cfg.get("capture", {}).get("warmup_seconds", 2)))
    print("counting — Ctrl+C to stop")
    try:
        while True:
            now = time.monotonic()
            capture_now = False
            if write_frames and now - last_cap_check > cap_check_s:
                last_cap_check = now
                capture_now = server_config.check_capture(srv_base, srv_device, srv_token)
            for cam in cameras:
                frame = cam.read()
                if frame is None:
                    continue
                tracked = trackers[cam.cam_id].update_with_detections(
                    to_sv(detector.detect(frame.image))
                )
                dets = [
                    (int(tid), foot_point(box))
                    for box, tid in zip(tracked.xyxy, tracked.tracker_id)
                ]
                counter = counters[cam.cam_id]
                evs = counter.update(dets, now)
                annotated = draw(frame.image, geometry.get(cam.cam_id, {}), tracked, counter, privacy, place)
                cv2.imwrite(str(ann / f"{cam.cam_id}.jpg"), annotated)

                if write_frames and (capture_now or now - last_ref.get(cam.cam_id, 0.0) > ref_interval):
                    ref = frame.image.copy()
                    obscure_heads(ref, tracked.xyxy, **privacy)  # never an identifiable face
                    ref = unrotate(ref, getattr(cam, "rotate", 0))  # raw orientation; editor rotates
                    cv2.imwrite(str(frames_dir / f"{cam.cam_id}.jpg"), ref)
                    last_ref[cam.cam_id] = now
                    if capture_now and srv_base and srv_token:
                        ok, buf = cv2.imencode(".jpg", ref, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                        if ok:
                            server_config.post_frame(srv_base, srv_device, cam.cam_id, buf.tobytes(), srv_token)

                box_by_track = {}
                if reid_store is not None and evs:
                    box_by_track = {int(t): b for b, t in zip(tracked.xyxy, tracked.tracker_id)}

                for e in evs:
                    db.execute(
                        "INSERT INTO events VALUES (?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"),
                         e.cam_id, e.type, e.track_id, round(e.dwell, 2)),
                    )
                    db.commit()
                    print(f"  >> {e.type.upper()} {cam.cam_id} #{e.track_id}"
                          + (f" dwell={e.dwell:.1f}s" if e.type == "stop" else ""))
                    snap_name = None
                    if snap_mode == "both" or e.type == snap_mode:
                        snap_name = save_event_snapshot(events_dir, e, annotated, snap_max)
                    if reid_store is not None:
                        box = box_by_track.get(e.track_id)
                        if box is not None:
                            info = reid.extract(frame.image, box, reid_mode)
                            if info:
                                m_t, m_d = reid.nearest(
                                    info["vec"], reid_store.recent(reid_window),
                                    exclude_track=e.track_id)
                                reid_store.add(
                                    datetime.now().isoformat(timespec="seconds"),
                                    e.cam_id, e.track_id, e.type, reid_mode, info,
                                    snap_name if reid_keep_img else None, m_t, m_d)

            if reid_store is not None and now - last_prune > 60:
                reid_store.prune()
                last_prune = now
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        for cam in cameras:
            cam.release()
        detector.close()
        db.close()
        if reid_store is not None:
            reid_store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
