"""Hailo detector — YOLOv8 on the AI HAT+ via picamera2's `Hailo` device class.

Mirrors raspberrypi/picamera2 examples/hailo/detect.py. The .hef is a pre-compiled
YOLOv8s for Hailo-8L (shipped with `hailo-all`, usually under /usr/share/hailo-models/).
The model has HailoRT NMS baked in, so `run()` returns, per class, a list of
[y0, x0, y1, x1, score] with normalized (0..1) coordinates.
"""
from __future__ import annotations

import cv2

from .base import Detection, Detector


class HailoDetector(Detector):
    def __init__(self, hef: str, confidence: float = 0.4, classes=(0,)) -> None:
        from picamera2.devices import Hailo  # lazy: Pi-only dep (ships with hailo-all)

        self._hailo = Hailo(hef)
        # input shape is (height, width, channels)
        self.model_h, self.model_w, *_ = self._hailo.get_input_shape()
        self._conf = confidence
        self._classes = set(classes)

    def detect(self, image) -> list[Detection]:
        h, w = image.shape[:2]
        resized = cv2.resize(image, (self.model_w, self.model_h))
        # Frames are BGR (OpenCV convention); the model wants RGB.
        # NOTE: if detection quality looks poor, this conversion is the first
        # thing to toggle — picamera2's "RGB888" channel order is famously quirky.
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        output = self._hailo.run(rgb)

        out: list[Detection] = []
        for class_id, dets in enumerate(output):
            if self._classes and class_id not in self._classes:
                continue
            for d in dets:
                score = float(d[4])
                if score < self._conf:
                    continue
                y0, x0, y1, x1 = d[0], d[1], d[2], d[3]
                out.append(Detection(x0 * w, y0 * h, x1 * w, y1 * h, score, class_id))
        return out

    def close(self) -> None:
        try:
            self._hailo.close()
        except Exception:
            pass
