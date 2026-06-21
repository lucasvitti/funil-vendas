"""Phase 2: per-camera person detection + tracking, with annotated previews.

For each configured camera it runs: capture -> detect -> ByteTrack -> draw boxes
+ track IDs -> save data/annotated/<cam_id>.jpg (overwritten each cycle, so you can
pull/serve it to eyeball accuracy headlessly). Prints per-camera counts + FPS.

    # Laptop dev (USB webcam + CPU YOLOv8):
    python detect_preview.py                # uses config.yaml

    # On the Pi (2 CSI cameras + Hailo):
    python detect_preview.py config.pi.yaml

Ctrl+C to stop.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import supervision as sv

from src.cameras import build_camera
from src.config import load_config
from src.detect import build_detector, to_sv
from src.privacy import obscure_heads


def main(config_path: str | None = None) -> int:
    cfg = load_config(config_path) if config_path else load_config()
    pcfg = cfg.get("privacy", {})
    privacy = {
        "mode": pcfg.get("face_mode", "pixelate"),
        "head_frac": float(pcfg.get("head_fraction", 0.28)),
        "blocks": int(pcfg.get("pixelate_blocks", 12)),
    }

    detector = build_detector(cfg.get("detection", {}))
    cameras = [build_camera(c) for c in cfg["cameras"]]
    for cam in cameras:
        cam.open()
        print(f"[{cam.cam_id}] opened")

    # One independent tracker per camera (per-camera sectors — no shared IDs).
    trackers = {cam.cam_id: sv.ByteTrack() for cam in cameras}

    warmup = float(cfg.get("capture", {}).get("warmup_seconds", 2))
    if warmup > 0:
        time.sleep(warmup)

    out_dir = Path("data/annotated")
    out_dir.mkdir(parents=True, exist_ok=True)
    quality = [cv2.IMWRITE_JPEG_QUALITY, int(cfg.get("capture", {}).get("jpeg_quality", 90))]
    throttle = float(cfg.get("capture", {}).get("interval_seconds", 0.0))

    print("running detection — Ctrl+C to stop")
    try:
        while True:
            t0 = time.monotonic()
            for cam in cameras:
                frame = cam.read()
                if frame is None:
                    print(f"  [{cam.cam_id}] no frame")
                    continue

                dets = detector.detect(frame.image)
                tracked = trackers[cam.cam_id].update_with_detections(to_sv(dets))

                img = frame.image.copy()
                obscure_heads(img, tracked.xyxy, **privacy)
                ids = []
                for xyxy, tid, conf in zip(
                    tracked.xyxy, tracked.tracker_id, tracked.confidence
                ):
                    x1, y1, x2, y2 = (int(v) for v in xyxy)
                    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(
                        img, f"#{tid} {conf:.2f}", (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA,
                    )
                    ids.append(int(tid))

                cv2.imwrite(str(out_dir / f"{cam.cam_id}.jpg"), img, quality)
                print(f"  [{cam.cam_id}] persons={len(tracked.xyxy)} ids={ids}")

            dt = time.monotonic() - t0
            fps = len(cameras) / dt if dt > 0 else 0.0
            print(f"cycle {dt * 1000:.0f} ms  (~{fps:.1f} cam-frames/s)")

            if throttle > 0:
                slack = throttle - dt
                if slack > 0:
                    time.sleep(slack)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        for cam in cameras:
            cam.release()
        detector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
