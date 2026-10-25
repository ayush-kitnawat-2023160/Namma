#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from huggingface_hub import snapshot_download

from common import DATA_ROOT, env, load_config


def main() -> None:
    cfg = load_config()["dataset"]
    repo_id = env("NP_DATASET_REPO", cfg["repo_id"])
    scene = env("NP_DATASET_SCENE", cfg["scene"])
    target = DATA_ROOT / "HIndoor-8K"

    print("=== NAMMA PANOGRAPH: DATASET FETCH ===")
    print(f"Dataset: {repo_id}")
    print(f"Scene:   {scene}")
    print("Policy:  RGB ONLY (LiDAR depth is deliberately not downloaded)")

    rgb_dir = target / scene / "rgb"
    # Permit a previously staged local dataset (for example, a public sample
    # whose host does not distribute through Hugging Face). This also avoids
    # re-downloading data already present in the shared workspace.
    existing_images = sorted(
        p for p in rgb_dir.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ) if rgb_dir.is_dir() else []
    if existing_images:
        print(f"Using staged local RGB panoramas: {rgb_dir}")
    else:
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            allow_patterns=[f"{scene}/rgb/*"],
            local_dir=str(target),
        )

    images = sorted(
        p for p in rgb_dir.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ) if rgb_dir.is_dir() else []
    if not images:
        raise RuntimeError(f"No RGB panoramas found after download: {rgb_dir}")

    manifest = {
        "repo_id": repo_id,
        "scene": scene,
        "rgb_only": True,
        "rgb_dir": str(rgb_dir),
        "images": [p.name for p in images],
        "count": len(images),
    }
    (target / f"{scene}_rgb_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
