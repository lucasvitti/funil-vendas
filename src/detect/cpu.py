"""CPU detector — Ultralytics YOLOv8. For laptop dev and as a fallback.

Ultralytics accepts a BGR numpy array directly (same convention as OpenCV).
"""
from __future__ import annotations

from .base import Detection, Detector


class CpuDetector(Detector):
    def __init__(self, model: str = "yolov8n.pt", confidence: float = 0.4, classes=(0,)) -> None:
        from ultralytics import YOLO  # lazy: heavy dep, only needed for this backend

        self._model = YOLO(model)
        self._conf = confidence
        self._classes = list(classes)

    def detect(self, image) -> list[Detection]:
        res = self._model.predict(
            image, conf=self._conf, classes=self._classes, verbose=False
        )[0]
        out: list[Detection] = []
        for b in res.boxes:
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            out.append(Detection(x1, y1, x2, y2, float(b.conf[0]), int(b.cls[0])))
        return out
