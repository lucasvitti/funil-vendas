"""Auto-find the correct `rotate` value per camera using the detector.

YOLO is trained on UPRIGHT people, so the rotation that yields the strongest
person detections is the upright one. This captures several frames per camera,
runs detection on all four rotations (0/90/180/270), sums the person-detection
confidence per rotation over the frames, and recommends the best.

A person MUST be in frame during this (stand in front, or run while someone passes).

    python calibrate_rotation.py config.pi.yaml
    # prints the recommended `rotate:` per camera + saves a labeled sheet.

It does NOT edit your config — set the recommended number in config.pi.yaml's
`rotate:` for each camera, then re-sync.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

from src.cameras import build_camera
from src.config import load_config
from src.detect import build_detector

_ROT = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
_FRAMES = 8


def _rotate(img, deg):
    return img if _ROT[deg] is None else cv2.rotate(img, _ROT[deg])


def _tile(raw, deg, score, is_best, tile_h=460):
    r = _rotate(raw, deg)
    scale = tile_h / r.shape[0]
    t = cv2.resize(r, (max(1, int(r.shape[1] * scale)), tile_h))
    cv2.rectangle(t, (0, 0), (t.shape[1], 40), (0, 0, 0), -1)
    cv2.putText(t, f"rotate: {deg}   score {score:.1f}", (12, 29),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA)
    if is_best:
        cv2.rectangle(t, (3, 3), (t.shape[1] - 4, t.shape[0] - 4), (0, 255, 0), 6)
        cv2.putText(t, "BEST", (t.shape[1] - 110, t.shape[0] - 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2, cv2.LINE_AA)
    return t


def _sheet(raw, scores, best):
    tiles = [_tile(raw, d, scores[d], d == best) for d in (0, 90, 180, 270)]
    w = max(t.shape[1] for t in tiles)
    tiles = [cv2.copyMakeBorder(t, 0, 0, 0, w - t.shape[1], cv2.BORDER_CONSTANT, value=(40, 40, 40)) for t in tiles]
    return np.vstack([np.hstack([tiles[0], tiles[1]]), np.hstack([tiles[2], tiles[3]])])


def main(config_path: str | None = None) -> int:
    cfg = load_config(config_path) if config_path else load_config()
    out = Path("data/calib")
    out.mkdir(parents=True, exist_ok=True)
    warmup = float(cfg.get("capture", {}).get("warmup_seconds", 2))
    detector = build_detector(cfg.get("detection", {}))

    try:
        for c in cfg["cameras"]:
            cam_cfg = dict(c)
            cam_cfg["rotate"] = 0  # raw orientation; we rotate in software here
            cam = build_camera(cam_cfg)
            cam.open()
            time.sleep(warmup)

            scores = {0: 0.0, 90: 0.0, 180: 0.0, 270: 0.0}
            last = None
            for _ in range(_FRAMES):
                frame = cam.read()
                if frame is None:
                    continue
                last = frame.image
                for deg in scores:
                    dets = detector.detect(_rotate(frame.image, deg))
                    scores[deg] += sum(d.score for d in dets)
                time.sleep(0.25)
            cam.release()

            if last is None:
                print(f"[{c['id']}] no frames captured")
                continue
            best = max(scores, key=scores.get)
            cv2.imwrite(str(out / f"{c['id']}_rotations.jpg"), _sheet(last, scores, best))
            ranked = ", ".join(f"{d}:{scores[d]:.1f}" for d in (0, 90, 180, 270))
            if scores[best] < 0.3:
                print(f"[{c['id']}] no person detected in any rotation — stand in frame and re-run. ({ranked})")
            else:
                print(f"[{c['id']}] >>> recommended rotate: {best}   (scores  {ranked})")
    finally:
        detector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
