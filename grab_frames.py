"""Capture one still per camera into data/calib/<cam_id>.jpg — the backdrop for
the Phase 3 geometry editor. Uses the same config as detection, so resolution +
rotation match exactly what the counting will see.

    python grab_frames.py config.pi.yaml
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2

from src.cameras import build_camera
from src.config import load_config


def main(config_path: str | None = None) -> int:
    cfg = load_config(config_path) if config_path else load_config()
    out = Path("data/calib")
    out.mkdir(parents=True, exist_ok=True)

    cameras = [build_camera(c) for c in cfg["cameras"]]
    for cam in cameras:
        cam.open()
    time.sleep(float(cfg.get("capture", {}).get("warmup_seconds", 2)))

    for cam in cameras:
        frame = cam.read()
        if frame is None:
            print(f"[{cam.cam_id}] no frame")
            continue
        path = out / f"{cam.cam_id}.jpg"
        cv2.imwrite(str(path), frame.image)
        h, w = frame.image.shape[:2]
        print(f"[{cam.cam_id}] saved {path}  ({w}x{h})")

    for cam in cameras:
        cam.release()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
