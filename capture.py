"""Phase 1 entry point: open all configured cameras and save near-synchronized
frame-sets to disk every `interval_seconds`.

This proves the camera + config layer works on whatever hardware you end up
buying. It runs on a Windows/Linux laptop webcam today and unchanged on the Pi.

    python capture.py [path/to/config.yaml]

Ctrl+C to stop.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

from src.cameras import build_camera
from src.config import load_config


def _timestamp() -> str:
    # Local wall-clock; only used to name folders, not for timing logic.
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def main(config_path: str | None = None) -> int:
    cfg = load_config(config_path) if config_path else load_config()
    cap_cfg = cfg.get("capture", {})
    interval = float(cap_cfg.get("interval_seconds", 5))
    warmup = float(cap_cfg.get("warmup_seconds", 2))
    quality = int(cap_cfg.get("jpeg_quality", 90))

    out_root = Path(cfg.get("output", {}).get("snapshots_dir", "data/snapshots"))
    out_root.mkdir(parents=True, exist_ok=True)

    cameras = [build_camera(c) for c in cfg["cameras"]]
    for cam in cameras:
        cam.open()
        print(f"[{cam.cam_id}] opened")

    if warmup > 0:
        print(f"warming up {warmup:.1f}s (auto-exposure settle)...")
        time.sleep(warmup)

    encode = [cv2.IMWRITE_JPEG_QUALITY, quality]
    print(f"capturing every {interval:.1f}s -> {out_root}  (Ctrl+C to stop)")

    try:
        while True:
            cycle_start = time.monotonic()

            # grab() every camera first (cheap latch) to minimize cross-camera
            # time skew, then retrieve() (decode) each.
            for cam in cameras:
                cam.grab()

            stamp = _timestamp()
            set_dir = out_root / stamp
            saved = 0
            for cam in cameras:
                frame = cam.retrieve()
                if frame is None:
                    print(f"  [{cam.cam_id}] no frame")
                    continue
                set_dir.mkdir(parents=True, exist_ok=True)
                path = set_dir / f"{cam.cam_id}.jpg"
                if cv2.imwrite(str(path), frame.image, encode):
                    saved += 1
            print(f"[{stamp}] saved {saved}/{len(cameras)} -> {set_dir}")

            # Keep a steady cadence regardless of capture cost.
            sleep_for = interval - (time.monotonic() - cycle_start)
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        for cam in cameras:
            cam.release()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
