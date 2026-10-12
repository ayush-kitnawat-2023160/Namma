#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np
import torch
import yaml
from geocalib import GeoCalib

from video_io import RawVideoReader


def sharpness(frame: np.ndarray) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    scale = min(1.0, 480.0 / max(gray.shape))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def select_calibration_frames(video: Path, views: int) -> tuple[list[np.ndarray], list[int], float, int, int]:
    cap = RawVideoReader(video)
    fps = cap.fps
    width = cap.width
    height = cap.height
    total = cap.total_frames
    if total <= 0 or fps <= 0:
        raise RuntimeError("Video frame count/FPS is invalid.")

    # Decode a bounded pool, then take the sharpest view from each temporal bin.
    pool_target = min(total, max(views * 8, views))
    stride = max(1, math.ceil(total / pool_target))
    candidates: list[tuple[int, float, np.ndarray]] = []
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % stride == 0:
            candidates.append((index, sharpness(frame), frame.copy()))
        index += 1
    cap.release()

    if len(candidates) < 3:
        raise RuntimeError("Need at least three decoded frames for automatic calibration.")
    views = min(max(3, views), len(candidates))
    edges = np.linspace(0, len(candidates), views + 1, dtype=int)
    selected = []
    for i in range(views):
        chunk = candidates[edges[i] : edges[i + 1]]
        if chunk:
            selected.append(max(chunk, key=lambda item: item[1]))
    return [item[2] for item in selected], [item[0] for item in selected], fps, width, height


def resize_for_calibration(frame: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    h, w = frame.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        frame = cv2.resize(
            frame,
            (round(w * scale), round(h * scale)),
            interpolation=cv2.INTER_AREA,
        )
    return frame, scale


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--views", type=int, default=20)
    parser.add_argument(
        "--camera-model",
        choices=["pinhole", "simple_radial", "radial"],
        default="radial",
    )
    parser.add_argument("--max-side", type=int, default=1280)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise RuntimeError("Automatic GeoCalib calibration requires CUDA in the NextGen pipeline.")

    frames, indices, fps, width, height = select_calibration_frames(args.video, args.views)
    resized = []
    scales = []
    for frame in frames:
        frame, scale = resize_for_calibration(frame, args.max_side)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        resized.append(torch.from_numpy(rgb).permute(2, 0, 1).float() / 255.0)
        scales.append(scale)
    if len({tuple(t.shape) for t in resized}) != 1:
        raise RuntimeError("Calibration frames unexpectedly have inconsistent shapes.")
    if max(scales) - min(scales) > 1e-9:
        raise RuntimeError("Calibration frame resize scale changed across a fixed-resolution video.")
    scale = scales[0]
    batch = torch.stack(resized).to("cuda")

    print(f"GeoCalib: {len(batch)} shared-intrinsic views, model={args.camera_model}, scale={scale:.6f}")
    model = GeoCalib(weights="distorted").to("cuda").eval()
    with torch.inference_mode():
        result = model.calibrate(
            batch,
            camera_model=args.camera_model,
            shared_intrinsics=True,
        )
    camera = result["camera"].cpu()

    f = camera.f.detach().numpy()
    c = camera.c.detach().numpy()
    fx = float(np.median(f[:, 0]) / scale)
    fy = float(np.median(f[:, 1]) / scale)
    cx = float(np.median(c[:, 0]) / scale)
    cy = float(np.median(c[:, 1]) / scale)
    k1 = float(np.median(camera.k1.detach().numpy())) if hasattr(camera, "k1") else 0.0
    k2 = float(np.median(camera.k2.detach().numpy())) if hasattr(camera, "k2") else 0.0

    values = np.array([fx, fy, cx, cy, k1, k2], dtype=np.float64)
    if not np.isfinite(values).all() or fx <= 0 or fy <= 0:
        raise RuntimeError(f"GeoCalib returned invalid camera parameters: {values}")

    profile = {
        "name": f"auto_geocalib_{args.camera_model}",
        "source": "GeoCalib shared-intrinsic RGB-video estimate",
        "width": width,
        "height": height,
        "fx": fx,
        "fy": fy,
        "cx": cx,
        "cy": cy,
        "distortion_model": "radtan",
        "distortion": [k1, k2, 0.0, 0.0],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as handle:
        yaml.safe_dump(profile, handle, sort_keys=False)

    focal_uncertainty = result.get("focal_uncertainty")
    uncertainty = None
    if focal_uncertainty is not None:
        uncertainty = float(torch.median(focal_uncertainty.detach().float().cpu()).item() / scale)
    report = {
        "video": str(args.video),
        "fps": fps,
        "resolution": [width, height],
        "camera_model": args.camera_model,
        "selected_source_frames": indices,
        "selected_timestamps": [idx / fps for idx in indices],
        "calibration_views": len(indices),
        "input_resize_scale": scale,
        "focal_uncertainty_pixels": uncertainty,
        "camera": profile,
        "notes": [
            "GeoCalib assumes the principal point is centered rather than freely optimizing it.",
            "The shared estimate is an initialization; LightGlue/COLMAP geometry validates downstream consistency.",
        ],
    }
    args.report.write_text(json.dumps(report, indent=2))
    print("=== AUTOMATIC RGB CALIBRATION COMPLETE ===")
    print(yaml.safe_dump(profile, sort_keys=False).strip())
    print("Profile:", args.output)
    print("Report: ", args.report)


if __name__ == "__main__":
    main()
