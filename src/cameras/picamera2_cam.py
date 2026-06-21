"""CSI camera via picamera2 (Raspberry Pi). Returns BGR frames for OpenCV.

`source` is the camera index (0 / 1 = the Pi 5's two CSI ports). Requires the
system `python3-picamera2` package; run inside a venv created with
`--system-site-packages` so the import resolves.
"""
from __future__ import annotations

import cv2

from .base import CameraSource, Frame

# Software rotation to make people upright when the module is mounted sideways.
_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


class Picamera2Camera(CameraSource):
    def __init__(self, cam_id, source=0, width=1536, height=864, fps=30, rotate=0) -> None:
        super().__init__(cam_id)
        self.index = int(source)
        self.width = width
        self.height = height
        self.fps = fps
        self.rotate = int(rotate) % 360
        self._cam = None

    def open(self) -> None:
        from picamera2 import Picamera2

        cam = Picamera2(self.index)
        cfg = cam.create_video_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"},
            controls={"FrameRate": float(self.fps)},
        )
        cam.configure(cfg)
        cam.start()
        self._cam = cam

    def grab(self) -> bool:
        return self._cam is not None

    def retrieve(self) -> Frame | None:
        if self._cam is None:
            return None
        # picamera2's "RGB888" already yields a BGR-ordered array (matches
        # OpenCV's convention), so we use it directly — no colour conversion.
        img = self._cam.capture_array("main")
        if self.rotate in _ROTATIONS:
            img = cv2.rotate(img, _ROTATIONS[self.rotate])
        return Frame(self.cam_id, img)

    def release(self) -> None:
        if self._cam is not None:
            self._cam.stop()
            self._cam.close()
            self._cam = None
