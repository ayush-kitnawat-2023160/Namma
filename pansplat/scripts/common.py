from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

CONFIG = Path('/opt/namma-pansplat/config/default.yaml')
WORKSPACE = Path('/workspace')
OUTPUT_ROOT = WORKSPACE / 'outputs'
CHECKPOINT_ROOT = WORKSPACE / 'checkpoints'


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {'1', 'true', 'yes', 'on'}


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def scene_root(scene: str) -> Path:
    return OUTPUT_ROOT / scene


def load_graph(scene: str) -> dict:
    path = scene_root(scene) / '03_graph' / 'graph.json'
    if not path.is_file():
        raise RuntimeError(f'PanoGraph graph missing: {path}')
    return json.loads(path.read_text())


def load_poses(scene: str) -> list[dict]:
    path = scene_root(scene) / '02_geometry' / 'poses.json'
    if not path.is_file():
        raise RuntimeError(f'PanoVGGT pose file missing: {path}')
    return json.loads(path.read_text())
