"""Camera sources + a factory that builds them from config dicts."""
from __future__ import annotations

from .base import CameraSource, Frame
from .usb import USBCamera

__all__ = ["CameraSource", "Frame", "build_camera"]


def build_camera(cfg: dict) -> CameraSource:
    """Construct a CameraSource from one `cameras:` entry in config.yaml."""
    cam_type = cfg.get("type", "usb")
    cam_id = cfg["id"]

    if cam_type == "usb":
        return USBCamera(
            cam_id=cam_id,
            source=cfg.get("source", 0),
            backend=cfg.get("backend", "auto"),
            width=cfg.get("width", 1280),
            height=cfg.get("height", 720),
            fps=cfg.get("fps", 30),
        )

    if cam_type == "picamera2":
        from .picamera2_cam import Picamera2Camera  # lazy: Pi-only dep

        return Picamera2Camera(
            cam_id=cam_id,
            source=cfg.get("source", 0),
            width=cfg.get("width", 1536),
            height=cfg.get("height", 864),
            fps=cfg.get("fps", 30),
            rotate=cfg.get("rotate", 0),
            exposure_us=cfg.get("exposure_us", 0),
            gain=cfg.get("gain", 0),
        )

    # ai_camera (IMX500) would land here as its own subclass if ever needed.
    raise NotImplementedError(
        f"[{cam_id}] camera type {cam_type!r} not implemented "
        f"(supported: 'usb', 'picamera2')."
    )
