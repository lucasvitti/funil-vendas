"""Detector abstraction: a backend takes a BGR frame and returns person boxes.

The pipeline only depends on this interface, so the CPU (laptop dev) and Hailo
(Pi production) backends are interchangeable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    class_id: int


class Detector(ABC):
    @abstractmethod
    def detect(self, image) -> list[Detection]:
        """Run detection on a BGR numpy frame; return kept Detections."""

    def close(self) -> None:
        """Release any backend resources."""
