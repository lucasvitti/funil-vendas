"""USB / generic OpenCV camera source (works on Windows dev and Linux/Pi)."""
from __future__ import annotations

import cv2

from .base import CameraSource, Frame

_BACKENDS = {
    "auto": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,   # Windows DirectShow
    "msmf": cv2.CAP_MSMF,     # Windows Media Foundation
    "v4l2": cv2.CAP_V4L2,     # Linux / Raspberry Pi
}


class USBCamera(CameraSource):
    def __init__(
        self,
        cam_id: str,
        source,
        backend: str = "auto",
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
    ) -> None:
        super().__init__(cam_id)
        self.source = source
        self.backend = backend
        self.width = width
        self.height = height
        self.fps = fps
        self._cap: cv2.VideoCapture | None = None

    def open(self) -> None:
        flag = _BACKENDS.get(self.backend, cv2.CAP_ANY)
        # A bare integer (or numeric string) is a device index; anything else is
        # treated as a path or RTSP/HTTP URL.
        src = self.source
        if isinstance(src, str) and src.isdigit():
            src = int(src)
        cap = cv2.VideoCapture(src, flag)
        if not cap.isOpened():
            raise RuntimeError(
                f"[{self.cam_id}] could not open source {self.source!r} "
                f"(backend={self.backend})"
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # keep latency low; ignored by some drivers
        self._cap = cap

    def grab(self) -> bool:
        return bool(self._cap and self._cap.grab())

    def retrieve(self) -> Frame | None:
        if self._cap is None:
            return None
        ok, image = self._cap.retrieve()
        return Frame(self.cam_id, image) if ok else None

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
