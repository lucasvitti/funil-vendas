"""Detector backends + a factory that builds them from the `detection:` config."""
from __future__ import annotations

import numpy as np

from .base import Detection, Detector

__all__ = ["Detection", "Detector", "build_detector", "to_sv"]


def build_detector(cfg: dict) -> Detector:
    backend = cfg.get("backend", "cpu")
    conf = float(cfg.get("confidence", 0.4))
    classes = tuple(cfg.get("classes", [0]))  # COCO ids; 0 = person

    if backend == "cpu":
        from .cpu import CpuDetector

        model = (cfg.get("cpu") or {}).get("model", "yolov8n.pt")
        return CpuDetector(model=model, confidence=conf, classes=classes)

    if backend == "hailo":
        from .hailo import HailoDetector

        hef = (cfg.get("hailo") or {}).get(
            "hef", "/usr/share/hailo-models/yolov8s_h8l.hef"
        )
        return HailoDetector(hef=hef, confidence=conf, classes=classes)

    raise ValueError(f"unknown detection backend: {backend!r}")


def to_sv(dets: list[Detection]):
    """Convert our Detections to a supervision.Detections for ByteTrack."""
    import supervision as sv

    if not dets:
        return sv.Detections.empty()
    xyxy = np.array([[d.x1, d.y1, d.x2, d.y2] for d in dets], dtype=float)
    conf = np.array([d.score for d in dets], dtype=float)
    cls = np.array([d.class_id for d in dets], dtype=int)
    return sv.Detections(xyxy=xyxy, confidence=conf, class_id=cls)
