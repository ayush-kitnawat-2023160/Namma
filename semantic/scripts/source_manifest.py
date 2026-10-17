#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


NEXTGEN_ROOT = Path("/workspace/nextgen")
SEMANTIC_ROOT = Path("/workspace/semantic")


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def count_files(path: Path, patterns: tuple[str, ...]) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for p in path.iterdir() if p.is_file() and p.suffix.lower() in patterns)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True)
    args = parser.parse_args()

    src = NEXTGEN_ROOT / args.scene
    dst = SEMANTIC_ROOT / args.scene / "00_source"

    required = {
        "frames": src / "01_frames/images",
        "vggt_cameras": src / "02_vggt/cameras.npz",
        "vggt_depths": src / "02_vggt/depths",
        "vggt_confidence": src / "02_vggt/confidence",
        "vggt_dense_cloud": src / "02_vggt/pointcloud.ply",
        "sfm_model": src / "03_sfm/model",
        "gs_dataset": src / "04_gs_dataset",
        "aligned_dense_cloud": src / "04_gs_dataset/sparse/0/points3D.ply",
    }

    missing = [f"{name}: {path}" for name, path in required.items() if not path.exists()]
    if missing:
        raise SystemExit(
            "NextGen scene is incomplete. Missing required semantic inputs:\n  - "
            + "\n  - ".join(missing)
        )

    frame_count = count_files(required["frames"], (".png", ".jpg", ".jpeg"))
    depth_count = count_files(required["vggt_depths"], (".png",))
    confidence_count = count_files(required["vggt_confidence"], (".npy",))
    if not (frame_count == depth_count == confidence_count):
        raise SystemExit(
            f"RGB/depth/confidence count mismatch: {frame_count}/{depth_count}/{confidence_count}"
        )

    vggt_summary = read_json(src / "02_vggt/summary.json")
    sfm_summary = read_json(src / "03_sfm/summary.json")
    gs_summary = read_json(src / "04_gs_dataset/summary.json")
    alignment = read_json(src / "04_gs_dataset/vggt_to_colmap_alignment.json")

    registered = int(sfm_summary.get("registered_images", 0))
    if registered and registered != frame_count:
        raise SystemExit(
            f"Expected all selected frames to be registered for semantic fusion; got {registered}/{frame_count}."
        )

    manifest = {
        "scene": args.scene,
        "source_mode": "read_only_nextgen",
        "source_root": str(src),
        "semantic_output_root": str(SEMANTIC_ROOT / args.scene),
        "counts": {
            "rgb_images": frame_count,
            "depth_maps": depth_count,
            "confidence_maps": confidence_count,
            "registered_images": registered,
            "sfm_points": int(sfm_summary.get("points3D", 0)),
            "dense_points": int(alignment.get("dense_points", vggt_summary.get("pointcloud_points", 0))),
        },
        "inputs": {name: str(path) for name, path in required.items()},
        "alignment": alignment,
        "vggt": vggt_summary,
        "sfm": sfm_summary,
        "gs_dataset": gs_summary,
        "stage_contract": {
            "01_segmentation": "2D stuff masks + object-instance masks",
            "02_semantic_cloud": "3D points carrying class, instance and confidence",
            "03_layout": "floor/wall/ceiling/pillar planes and room shell",
            "04_object_meshes": "one observed-surface mesh per object instance",
            "05_textures": "RGB texture/vertex-color baking from source views",
            "06_scene": "scene graph + visual meshes + collision meshes + GLB/USD",
            "07_viewer": "first-person package using the explicit scene geometry",
        },
    }

    dst.mkdir(parents=True, exist_ok=True)
    out = dst / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2))

    print("=== SEMANTIC SOURCE READY ===")
    print(f"Scene:              {args.scene}")
    print(f"RGB/depth/conf:     {frame_count}/{depth_count}/{confidence_count}")
    print(f"Registered cameras: {registered or 'unknown'}")
    print(f"Dense points:       {manifest['counts']['dense_points']}")
    print(f"Manifest:           {out}")
    print("NextGen source remains read-only and unchanged.")


if __name__ == "__main__":
    main()
