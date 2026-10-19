#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

import yaml


CONFIG_FILE = Path("/opt/namma-panograph/config/default.yaml")
DATA_ROOT = Path("/workspace/data")
OUTPUT_ROOT = Path("/workspace/outputs")
CHECKPOINT_ROOT = Path("/workspace/checkpoints")


def load_config() -> dict:
    with CONFIG_FILE.open() as handle:
        return yaml.safe_load(handle) or {}


def env(name: str, default=None):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name: str, default: int) -> int:
    return int(env(name, default))


def env_float(name: str, default: float) -> float:
    return float(env(name, default))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def scene_root(scene: str) -> Path:
    return OUTPUT_ROOT / scene


def ensure_clean_dir(path: Path, overwrite: bool) -> None:
    import shutil

    if path.exists() and overwrite:
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
