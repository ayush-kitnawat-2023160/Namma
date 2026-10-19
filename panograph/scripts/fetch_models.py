#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from huggingface_hub import hf_hub_download

from common import CHECKPOINT_ROOT, env, load_config


def main() -> None:
    cfg = load_config()["panovggt"]
    repo_id = cfg["checkpoint_repo"]
    filename = cfg["checkpoint_file"]
    target_dir = CHECKPOINT_ROOT / "PanoVGGT"
    target_dir.mkdir(parents=True, exist_ok=True)

    override = env("NP_PANOVGGT_CHECKPOINT")
    if override:
        checkpoint = Path(override)
        if not checkpoint.is_file():
            raise RuntimeError(f"NP_PANOVGGT_CHECKPOINT does not exist: {checkpoint}")
    else:
        checkpoint = Path(
            hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=str(target_dir),
            )
        )

    if checkpoint.stat().st_size < 1_000_000:
        raise RuntimeError(f"Downloaded checkpoint is suspiciously small: {checkpoint}")

    manifest = {
        "model": "PanoVGGT",
        "repo_id": repo_id,
        "checkpoint": str(checkpoint),
        "bytes": checkpoint.stat().st_size,
    }
    (target_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("=== NAMMA PANOGRAPH: MODEL READY ===")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
