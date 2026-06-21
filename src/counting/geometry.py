"""Geometry primitives for counting: load geometry.yaml + the two core tests.

- side_of_line: which side of the tripwire a foot point is on (sign of the 2D
  cross product). A sign flip between frames = the person crossed the line.
- point_in_polygon: is the foot point inside the dwell zone (ray casting).
"""
from __future__ import annotations

from pathlib import Path

import yaml


def load_geometry(path: str | Path) -> dict:
    """Return {cam_id: {'tripwire': [[x,y],[x,y]], 'zone': [[x,y],...]}}."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("geometry", {}) or {}


def side_of_line(a, b, p) -> float:
    """Cross product (b-a) x (p-a). >0 one side, <0 the other, 0 on the line."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def point_in_polygon(poly, p) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = p
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside
