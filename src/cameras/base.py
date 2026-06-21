"""Camera-source abstraction.

The pipeline never talks to a specific camera API directly — it works against
``CameraSource``. USB (OpenCV) is implemented for Phase 1; ``picamera2`` and the
IMX500 ``ai_camera`` become sibling subclasses later, with no pipeline changes.

The ``grab()`` / ``retrieve()`` split exists so the capture loop can ``grab()``
every camera back-to-back (cheap, just latches the latest frame) and only then
``retrieve()`` (the expensive decode). That keeps inter-camera time skew small —
important once three angles of the same moving person have to be fused.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class Frame:
    cam_id: str
    image: np.ndarray


class CameraSource(ABC):
    def __init__(self, cam_id: str) -> None:
        self.cam_id = cam_id

    @abstractmethod
    def open(self) -> None:
        """Acquire the device. Raise on failure."""

    @abstractmethod
    def grab(self) -> bool:
        """Cheaply latch the next frame without decoding it. Returns success."""

    @abstractmethod
    def retrieve(self) -> Frame | None:
        """Decode and return the frame latched by the last ``grab()``."""

    @abstractmethod
    def release(self) -> None:
        """Release the device."""

    def read(self) -> Frame | None:
        """Convenience single-camera read (grab + retrieve)."""
        return self.retrieve() if self.grab() else None

    def __enter__(self) -> "CameraSource":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
