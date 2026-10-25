#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image
from plyfile import PlyData, PlyElement
from vggt_omega.models import VGGTOmega
from vggt_omega.utils.load_fn import load_and_preprocess_images
from vggt_omega.utils.pose_enc import encoding_to_camera


def squeeze_batch(value: np.ndarray) -> np.ndarray:
    if value.ndim > 0 and value.shape[0] == 1:
        return value[0]
    return value


def unproject_depth(depth_map: np.ndarray, extrinsic: np.ndarray, intrinsic: np.ndarray) -> np.ndarray:
    depth = depth_map[..., 0] if depth_map.ndim == 4 else depth_map
    num_frames, height, width = depth.shape
    y, x = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    x = np.broadcast_to(x[None], (num_frames, height, width))
    y = np.broadcast_to(y[None], (num_frames, height, width))
    fx = intrinsic[:, 0, 0][:, None, None]
    fy = intrinsic[:, 1, 1][:, None, None]
    cx = intrinsic[:, 0, 2][:, None, None]
    cy = intrinsic[:, 1, 2][:, None, None]
    camera_points = np.stack(
        [(x - cx) / fx * depth, (y - cy) / fy * depth, depth], axis=-1
    )
    rotation = extrinsic[:, :3, :3]
    translation = extrinsic[:, :3, 3]
    return np.einsum(
        "sij,shwj->shwi",
        np.transpose(rotation, (0, 2, 1)),
        camera_points - translation[:, None, None, :],
    )


def save_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> None:
    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("nx", "f4"), ("ny", "f4"), ("nz", "f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
    ]
    vertices = np.empty(len(xyz), dtype=dtype)
    normals = np.zeros_like(xyz, dtype=np.float32)
    attrs = np.concatenate([xyz.astype(np.float32), normals, rgb.astype(np.uint8)], axis=1)
    vertices[:] = list(map(tuple, attrs))
    path.parent.mkdir(parents=True, exist_ok=True)
    PlyData([PlyElement.describe(vertices, "vertex")], text=False).write(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--resolution", type=int, default=512)
    parser.add_argument("--mode", choices=["balanced", "max_size"], default="max_size")
    parser.add_argument("--confidence-percentile", type=float, default=20.0)
    parser.add_argument("--max-points", type=int, default=1_000_000)
    parser.add_argument("--gpu-memory-fraction", type=float, default=0.90)
    parser.add_argument("--gpu-headroom-gb", type=float, default=1.5)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise RuntimeError("VGGT-Omega stage requires CUDA.")
    if not 0.20 <= args.gpu_memory_fraction <= 0.98:
        raise RuntimeError(f"Invalid --gpu-memory-fraction={args.gpu_memory_fraction}")
    torch.cuda.set_per_process_memory_fraction(args.gpu_memory_fraction, device=0)
    image_paths = sorted(
        p for p in args.images.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if not image_paths:
        raise RuntimeError(f"No input images found in {args.images}")

    free_bytes, total_bytes = torch.cuda.mem_get_info(0)
    free_gb = free_bytes / (1024 ** 3)
    total_gb = total_bytes / (1024 ** 3)
    # Conservative planning model based on published VGGT-Omega max-size memory
    # measurements: ~9.7 GiB/50 frames, 13.4/100, 20.8/200.  This is only a
    # guardrail; the allocator fraction below is the hard process-side cap.
    estimated_gb = 5.95 + 0.0745 * len(image_paths)
    budget_gb = min(
        total_gb * args.gpu_memory_fraction,
        max(0.0, free_gb - args.gpu_headroom_gb),
    )
    print(
        f"GPU planning: {len(image_paths)} views, estimated~{estimated_gb:.1f} GiB, "
        f"safe budget~{budget_gb:.1f} GiB (free {free_gb:.1f}/{total_gb:.1f} GiB)"
    )
    if estimated_gb > budget_gb:
        safe_views = max(1, int((budget_gb - 5.95) / 0.0745))
        raise RuntimeError(
            f"Refusing risky VGGT-Omega run: {len(image_paths)} views are estimated to need "
            f"~{estimated_gb:.1f} GiB, above the {budget_gb:.1f} GiB safety budget. "
            f"Re-run prepare with NG_TARGET_VIEWS<={safe_views} or free more VRAM."
        )

    with Image.open(image_paths[0]) as first:
        original_size = first.size
    for path in image_paths[1:]:
        with Image.open(path) as image:
            if image.size != original_size:
                raise RuntimeError("All selected video views must have the same image size.")
    aspect = original_size[1] / max(original_size[0], 1)
    if not (0.5 <= aspect <= 2.0):
        raise RuntimeError(
            "VGGT-Omega would center-crop this extreme aspect ratio, which would break "
            "pixel-aligned depth priors. Pre-crop the video explicitly first."
        )

    args.output.mkdir(parents=True, exist_ok=True)
    depth_dir = args.output / "depths"
    conf_dir = args.output / "confidence"
    model_image_dir = args.output / "model_images"
    depth_dir.mkdir(exist_ok=True)
    conf_dir.mkdir(exist_ok=True)
    model_image_dir.mkdir(exist_ok=True)

    print(f"Loading {len(image_paths)} selected views...")
    images = load_and_preprocess_images(
        [str(p) for p in image_paths],
        mode=args.mode,
        image_resolution=args.resolution,
    ).to("cuda")
    print("VGGT-Omega tensor:", tuple(images.shape))

    print("Loading checkpoint:", args.checkpoint)
    model = VGGTOmega().eval()
    state_dict = torch.load(args.checkpoint, map_location="cpu")
    model.load_state_dict(state_dict)
    model = model.to("cuda")

    with torch.inference_mode():
        predictions = model(images)
        extrinsic, intrinsic = encoding_to_camera(
            predictions["pose_enc"], predictions["images"].shape[-2:]
        )

    pred_images = squeeze_batch(predictions["images"].detach().float().cpu().numpy())
    depth = squeeze_batch(predictions["depth"].detach().float().cpu().numpy())
    confidence = squeeze_batch(predictions["depth_conf"].detach().float().cpu().numpy())
    extrinsic = squeeze_batch(extrinsic.detach().float().cpu().numpy())
    intrinsic = squeeze_batch(intrinsic.detach().float().cpu().numpy())
    del predictions, images, model
    torch.cuda.empty_cache()

    if depth.ndim == 4 and depth.shape[-1] == 1:
        depth_scalar = depth[..., 0]
    else:
        depth_scalar = depth
    if confidence.ndim == 4 and confidence.shape[-1] == 1:
        confidence_scalar = confidence[..., 0]
    else:
        confidence_scalar = confidence

    world_points = unproject_depth(depth, extrinsic, intrinsic)

    np.savez_compressed(
        args.output / "cameras.npz",
        image_names=np.array([p.name for p in image_paths]),
        extrinsic=extrinsic,
        intrinsic=intrinsic,
        model_height=np.int32(depth_scalar.shape[-2]),
        model_width=np.int32(depth_scalar.shape[-1]),
        original_width=np.int32(original_size[0]),
        original_height=np.int32(original_size[1]),
    )

    valid_inv = np.where(depth_scalar > 1e-6, 1.0 / depth_scalar, 0.0)
    finite_positive = valid_inv[np.isfinite(valid_inv) & (valid_inv > 0)]
    if finite_positive.size == 0:
        raise RuntimeError("VGGT-Omega produced no valid positive depth.")
    p999 = float(np.percentile(finite_positive, 99.9))
    encode_scale = min(8192.0, 65000.0 / max(p999, 1e-6))

    for idx, path in enumerate(image_paths):
        inv = np.where(np.isfinite(valid_inv[idx]), valid_inv[idx], 0.0)
        encoded = np.clip(inv * encode_scale, 0, 65535).astype(np.uint16)
        cv2.imwrite(str(depth_dir / f"{path.stem}.png"), encoded)
        np.save(conf_dir / f"{path.stem}.npy", confidence_scalar[idx].astype(np.float16))

    if pred_images.ndim == 4 and pred_images.shape[1] == 3:
        colors = np.transpose(pred_images, (0, 2, 3, 1))
    else:
        colors = pred_images
    colors = np.clip(colors * 255.0, 0, 255).astype(np.uint8)

    for idx, path in enumerate(image_paths):
        # Keep the exact pixel-aligned image used by VGGT-Omega.  Later geometry
        # fusion must not guess how max_size preprocessing resized/cropped RGB.
        cv2.imwrite(
            str(model_image_dir / f"{path.stem}.png"),
            cv2.cvtColor(colors[idx], cv2.COLOR_RGB2BGR),
        )

    xyz = world_points.reshape(-1, 3)
    rgb = colors.reshape(-1, 3)
    conf = confidence_scalar.reshape(-1)
    mask = np.isfinite(xyz).all(axis=1) & np.isfinite(conf)
    if np.any(mask):
        threshold = np.percentile(conf[mask], args.confidence_percentile)
        mask &= conf >= threshold
        mask &= conf > 1e-5
    xyz = xyz[mask]
    rgb = rgb[mask]
    if len(xyz) > args.max_points:
        indices = np.linspace(0, len(xyz) - 1, args.max_points).astype(np.int64)
        xyz = xyz[indices]
        rgb = rgb[indices]
    save_ply(args.output / "pointcloud.ply", xyz, rgb)

    camera_centers = []
    for ext in extrinsic:
        R = ext[:3, :3]
        t = ext[:3, 3]
        camera_centers.append((-R.T @ t).tolist())

    summary = {
        "images": len(image_paths),
        "image_names": [p.name for p in image_paths],
        "original_size": [original_size[0], original_size[1]],
        "model_size": [int(depth_scalar.shape[-1]), int(depth_scalar.shape[-2])],
        "resize_mode": args.mode,
        "image_resolution": args.resolution,
        "inverse_depth_encode_scale": encode_scale,
        "confidence_percentile": args.confidence_percentile,
        "gpu_memory_fraction": args.gpu_memory_fraction,
        "pointcloud_points": int(len(xyz)),
        "camera_centers": camera_centers,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print("=== VGGT-OMEGA COMPLETE ===")
    print("Cameras:", args.output / "cameras.npz")
    print("Depth priors:", depth_dir)
    print("Dense point cloud:", args.output / "pointcloud.ply")


if __name__ == "__main__":
    main()
