"""Load and lightly validate config.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path = DEFAULT_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    cameras = cfg.get("cameras") or []
    if not cameras:
        raise ValueError("config.yaml has no `cameras:` entries")

    ids = [c.get("id") for c in cameras]
    if any(not i for i in ids):
        raise ValueError("every camera needs an `id`")
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate camera ids: {ids}")

    return cfg
