"""Detector backends + a factory that builds them from the `detection:` config."""
from __future__ import annotations

import numpy as np

from .base import Detection, Detector

__all__ = ["Detection", "Detector", "build_detector", "to_sv", "coco_name", "resolve_classes"]

# COCO 80 class names, index = class id.
COCO_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis",
    "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork", "knife",
    "spoon", "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed",
    "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)


def coco_name(i) -> str:
    i = int(i)
    return COCO_NAMES[i] if 0 <= i < len(COCO_NAMES) else str(i)


def resolve_classes(spec) -> tuple:
    """Accept a list/str/int of COCO ids and/or names -> tuple of ids (default person)."""
    if not spec:
        return (0,)
    if isinstance(spec, str):
        spec = [s.strip() for s in spec.replace(";", ",").split(",") if s.strip()]
    elif isinstance(spec, int):
        spec = [spec]
    out = []
    for x in spec:
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, str) and x.strip().isdigit():
            out.append(int(x))
        elif isinstance(x, str) and x.strip().lower() in COCO_NAMES:
            out.append(COCO_NAMES.index(x.strip().lower()))
    return tuple(dict.fromkeys(out)) if out else (0,)   # dedupe, preserve order


def build_detector(cfg: dict) -> Detector:
    backend = cfg.get("backend", "cpu")
    conf = float(cfg.get("confidence", 0.4))
    classes = resolve_classes(cfg.get("detect", cfg.get("classes", [0])))  # names or COCO ids

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
