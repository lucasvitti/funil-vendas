"""Pull per-device config (tunable params + counting geometry) from the topofunil
server at boot, and overlay it onto the local config. Stdlib only. Any failure
(server disabled, unreachable, no token, 404) falls back silently to local config.

    doc = fetch_config(base_url, device)        # {"params": {...}, "geometry": {...}} or None
    geom = apply_config(cfg, doc)               # mutates cfg params; returns geometry override or None
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path


def _token():
    t = os.environ.get("COUNTER_TOKEN")
    if t:
        return t.strip()
    f = Path.home() / ".counter_token"
    return f.read_text().strip() if f.exists() else None


def fetch_config(base, device, token=None, timeout=10):
    """Return the server config doc for this device, or None on any failure."""
    token = token or _token()
    if not base or not token:
        return None
    url = f"{base.rstrip('/')}/api/config/{device}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as ex:
        print(f"  server config: using local fallback ({ex})")
        return None


def check_capture(base, device, token=None, timeout=8):
    """Return True if the editor requested an on-demand 'take shot' (consumes it)."""
    token = token or _token()
    if not base or not token:
        return False
    url = f"{base.rstrip('/')}/api/capture/{device}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.load(resp).get("pending"))
    except Exception:
        return False


def check_restart(base, device, token=None, timeout=8):
    """Return the server's restart watermark (epoch float) for this device, or 0.0.
    The board records this at startup and, when a later poll returns a newer value,
    exits so systemd relaunches it — a clean way to re-pull config + geometry."""
    token = token or _token()
    if not base or not token:
        return 0.0
    url = f"{base.rstrip('/')}/api/restart/{device}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return float(json.load(resp).get("at") or 0)
    except Exception:
        return 0.0


def post_frame(base, device, cam, jpg_bytes, token=None, timeout=15):
    """Upload one reference frame immediately (used for on-demand 'take shot')."""
    token = token or _token()
    if not base or not token:
        return False
    body = json.dumps({"device": device, "cam": cam,
                       "image_b64": base64.b64encode(jpg_bytes).decode()}).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/frame", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as ex:
        print(f"  on-demand frame upload failed: {ex}")
        return False


# server param name -> (cfg section, key)
_PARAM_MAP = {
    "place_name": ("location", "name"),
    "dwell_seconds": ("counting", "dwell_seconds"),
    "track_cooldown_s": ("counting", "track_cooldown_s"),
    "cross_mode": ("counting", "cross_mode"),
    "log_crossings": ("counting", "log_crossings"),
    "confidence": ("detection", "confidence"),
    "detect": ("detection", "detect"),
    "face_mode": ("privacy", "face_mode"),
    "reid_enabled": ("reid", "enabled"),
    "reid_mode": ("reid", "mode"),
    "compare_window_s": ("reid", "compare_window_s"),
    "retention_hours": ("reid", "retention_hours"),
    "match_threshold": ("reid", "match_threshold"),
    "store_images": ("reid", "store_images"),
    "upload_events": ("server", "upload_events"),
    "upload_vectors": ("server", "upload_vectors"),
    "upload_images": ("server", "upload_images"),
    "capture_poll_s": ("server", "capture_poll_s"),
    "reference_frame_s": ("server", "reference_frame_s"),
    "exposure_us": ("capture", "exposure_us"),
    "gain": ("capture", "gain"),
}


def apply_config(cfg, doc):
    """Overlay server params onto cfg in place. Return the server geometry dict
    (to use instead of the local geometry.yaml), or None if none was provided."""
    if not doc:
        return None
    params = doc.get("params") or {}
    applied = []
    for name, val in params.items():
        if val is None or name not in _PARAM_MAP:
            continue
        section, key = _PARAM_MAP[name]
        cfg.setdefault(section, {})[key] = val
        applied.append(name)
    if applied:
        print(f"  server config: applied {len(applied)} params -> {', '.join(applied)}")
    geom = doc.get("geometry") or None
    if geom:
        # geometry may carry a per-camera rotate (set in the editor) — push it
        # onto the matching camera config so the board captures upright.
        cams = {c.get("id"): c for c in cfg.get("cameras", [])}
        for cam_id, g in geom.items():
            if isinstance(g, dict) and "rotate" in g and cam_id in cams:
                cams[cam_id]["rotate"] = int(g["rotate"]) % 360
        print(f"  server config: geometry for {', '.join(geom.keys())}")
    return geom
