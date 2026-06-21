"""Person re-identification (re-ID) — PILOT/study scaffolding.

At each count-event we extract an appearance vector for the person, record it with
quality metadata in a local SQLite DB, and find the nearest recent vector. It only
RECORDS the match distance — it does not suppress counts yet. First gather data,
study the same-person vs different-person distance distributions (reid_study.py),
set a threshold, then enable dedup.

LGPD: `body` mode (clothing, head excluded) avoids facial biometrics and pairs with
the face pixelation. `face`/`full` modes involve facial biometric data
(dado pessoal sensivel) — gated, internal office pilot only, colleagues informed,
DPO review before any wider use. The histogram is a placeholder appearance feature;
swap in a learned re-ID/face embedding behind `extract()` when ready.
"""
from __future__ import annotations

import sqlite3
import time

import cv2
import numpy as np


# ---------- quality metadata ----------
def _clarity(crop) -> float:
    """Sharpness = variance of the Laplacian (higher = sharper)."""
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# ---------- appearance vector ----------
def _hsv_hist(crop, bins=(8, 8, 8)) -> np.ndarray:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, bins, [0, 180, 0, 256, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten().astype(np.float32)


def extract(image, box, mode="body", margin=4):
    """Return {vec, clarity, face_visible, body_visible} for a person box, or None.

    mode: body (clothing, head excluded) | face (head region) | full (whole box).
    """
    h, w = image.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in box)
    cx1, cy1, cx2, cy2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if cx2 <= cx1 or cy2 <= cy1:
        return None
    full = image[cy1:cy2, cx1:cx2]
    head_h = int((cy2 - cy1) * 0.22)
    if mode == "face":
        crop = image[cy1:cy1 + head_h, cx1:cx2]
    elif mode == "full":
        crop = full
    else:  # body
        crop = image[cy1 + head_h:cy2, cx1:cx2]
    if crop.size == 0:
        return None
    return {
        "vec": _hsv_hist(crop),
        "clarity": round(_clarity(full), 1),
        # head not clipped by the top/side edges:
        "face_visible": int(y1 > margin and x1 > margin and x2 < w - margin),
        # whole body inside the frame:
        "body_visible": int(x1 > margin and y1 > margin and x2 < w - margin and y2 < h - margin),
    }


def distance(a, b) -> float:
    """Cosine distance in [0, 2]; 0 = identical."""
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 2.0
    return float(1.0 - np.dot(a, b) / (na * nb))


def nearest(vec, recent, exclude_track=None):
    """recent: list of (track, vec). Returns (best_track, best_distance) or (None, None)."""
    best_t, best_d = None, None
    for t, v in recent:
        if exclude_track is not None and t == exclude_track:
            continue
        d = distance(vec, v)
        if best_d is None or d < best_d:
            best_t, best_d = t, d
    return best_t, best_d


# ---------- local store ----------
class ReidStore:
    def __init__(self, path, retention_hours=24):
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS vectors(
                t REAL, ts TEXT, cam TEXT, track INTEGER, event TEXT, mode TEXT,
                vec BLOB, clarity REAL, face_visible INTEGER, body_visible INTEGER,
                image TEXT, match_track INTEGER, match_dist REAL)"""
        )
        self.db.commit()
        self.retention_s = retention_hours * 3600

    def recent(self, window_s):
        cutoff = time.time() - window_s
        rows = self.db.execute(
            "SELECT track, vec FROM vectors WHERE t >= ?", (cutoff,)
        ).fetchall()
        return [(r[0], np.frombuffer(r[1], dtype=np.float32)) for r in rows]

    def add(self, ts, cam, track, event, mode, info, image, m_track, m_dist):
        self.db.execute(
            "INSERT INTO vectors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), ts, cam, track, event, mode, info["vec"].tobytes(),
             info["clarity"], info["face_visible"], info["body_visible"],
             image, m_track, None if m_dist is None else round(m_dist, 4)),
        )
        self.db.commit()

    def prune(self):
        self.db.execute("DELETE FROM vectors WHERE t < ?", (time.time() - self.retention_s,))
        self.db.commit()

    def close(self):
        self.db.close()
