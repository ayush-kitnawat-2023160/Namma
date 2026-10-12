#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml


@dataclass
class Candidate:
    frame_index: int
    timestamp: float
    blur: float
    mean_intensity: float
    dark_clip: float
    bright_clip: float
    score: float
    descriptor: np.ndarray


def open_video(video: Path) -> cv2.VideoCapture:
    """Open the coded video stream without applying display-rotation metadata.

    ADVIO calibration is defined in the sensor/coded-frame orientation
    (720x1280). OpenCV/FFmpeg may otherwise auto-rotate the stream to 1280x720
    using container metadata, which makes the calibration appear incompatible.
    Keeping the coded orientation also avoids having to rotate tangential
    distortion coefficients.
    """
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video}")

    orientation_auto = getattr(cv2, "CAP_PROP_ORIENTATION_AUTO", None)
    if orientation_auto is not None:
        cap.set(orientation_auto, 0)
    return cap


def load_profile(path: Path, width: int, height: int):
    with path.open() as handle:
        profile = yaml.safe_load(handle)

    ref_w = int(profile["width"])
    ref_h = int(profile["height"])
    if (ref_w, ref_h) != (width, height):
        raise RuntimeError(
            f"Camera profile is {ref_w}x{ref_h} but video is {width}x{height}. "
            "Use a matching profile or explicitly rescale/calibrate it first."
        )

    K = np.array(
        [
            [float(profile["fx"]), 0.0, float(profile["cx"])],
            [0.0, float(profile["fy"]), float(profile["cy"])],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    D = np.asarray(profile.get("distortion", [0, 0, 0, 0]), dtype=np.float64)
    new_k, roi = cv2.getOptimalNewCameraMatrix(K, D, (width, height), 0, (width, height))
    map1, map2 = cv2.initUndistortRectifyMap(
        K, D, None, new_k, (width, height), cv2.CV_32FC1
    )
    return profile, K, D, new_k, roi, map1, map2


def preprocess_for_metrics(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    scale = min(1.0, 480.0 / max(h, w))
    if scale < 1.0:
        gray = cv2.resize(gray, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    descriptor = cv2.resize(gray, (32, 18), interpolation=cv2.INTER_AREA).astype(np.float32)
    descriptor = (descriptor - descriptor.mean()) / (descriptor.std() + 1e-6)
    return gray, descriptor.reshape(-1)


def metrics(frame: np.ndarray) -> tuple[float, float, float, float, float, np.ndarray]:
    gray, descriptor = preprocess_for_metrics(frame)
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    dark = float((gray <= 8).mean())
    bright = float((gray >= 247).mean())
    exposure_penalty = abs(mean - 127.5) / 127.5
    score = math.log1p(max(blur, 0.0)) - 1.2 * exposure_penalty - 3.0 * (dark + bright)
    return blur, mean, dark, bright, score, descriptor


def descriptor_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a - b)))


def select_candidates(
    candidates: list[Candidate],
    target: int,
    min_blur: float,
    max_clip_fraction: float,
    duplicate_distance: float,
) -> list[Candidate]:
    if not candidates:
        raise RuntimeError("No candidate frames were decoded from the video.")

    valid = [
        c
        for c in candidates
        if c.blur >= min_blur and c.dark_clip <= max_clip_fraction and c.bright_clip <= max_clip_fraction
    ]
    if len(valid) < max(8, min(target, len(candidates)) // 2):
        print(
            f"[WARN] Strict quality filter retained only {len(valid)}/{len(candidates)} candidates; "
            "falling back to ranked candidates so temporal coverage is not lost."
        )
        valid = candidates

    target = max(1, min(target, len(valid)))
    edges = np.linspace(0, len(valid), target + 1, dtype=int)
    selected: list[Candidate] = []

    for bin_idx in range(target):
        chunk = valid[edges[bin_idx] : edges[bin_idx + 1]]
        if not chunk:
            continue
        ranked = sorted(chunk, key=lambda c: c.score, reverse=True)
        chosen = ranked[0]
        if selected:
            recent = selected[-min(3, len(selected)) :]
            for proposal in ranked:
                novelty = min(descriptor_distance(proposal.descriptor, old.descriptor) for old in recent)
                if novelty >= duplicate_distance:
                    chosen = proposal
                    break
        selected.append(chosen)

    selected.sort(key=lambda c: c.frame_index)
    return selected


def extract_selected(
    video: Path,
    selected: list[Candidate],
    out_images: Path,
    map1: np.ndarray | None,
    map2: np.ndarray | None,
) -> list[dict]:
    wanted = {c.frame_index: (i, c) for i, c in enumerate(selected)}
    cap = open_video(video)

    records: list[dict] = []
    frame_idx = 0
    while wanted:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx in wanted:
            output_idx, candidate = wanted.pop(frame_idx)
            if map1 is not None:
                frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
            name = f"{output_idx:06d}.png"
            if not cv2.imwrite(str(out_images / name), frame):
                raise RuntimeError(f"Failed to write {name}")
            records.append(
                {
                    "name": name,
                    "source_frame": candidate.frame_index,
                    "timestamp": candidate.timestamp,
                    "blur": candidate.blur,
                    "mean_intensity": candidate.mean_intensity,
                    "dark_clip_fraction": candidate.dark_clip,
                    "bright_clip_fraction": candidate.bright_clip,
                    "quality_score": candidate.score,
                }
            )
        frame_idx += 1
    cap.release()

    if wanted:
        raise RuntimeError(f"Could not decode {len(wanted)} selected source frames.")
    records.sort(key=lambda item: item["name"])
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--camera-profile", type=Path)
    parser.add_argument("--target-views", type=int, default=120)
    parser.add_argument("--max-candidates", type=int, default=1200)
    parser.add_argument("--min-blur", type=float, default=20.0)
    parser.add_argument("--max-clip-fraction", type=float, default=0.45)
    parser.add_argument("--duplicate-distance", type=float, default=0.025)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Output exists: {args.output}")
        shutil.rmtree(args.output)
    image_dir = args.output / "images"
    image_dir.mkdir(parents=True)

    cap = open_video(args.video)
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps <= 0:
        raise RuntimeError("Video FPS is invalid.")

    map1 = map2 = None
    camera_report: dict = {"known_calibration": False}
    if args.camera_profile:
        profile, K, D, new_k, roi, map1, map2 = load_profile(args.camera_profile, width, height)
        camera_report = {
            "known_calibration": True,
            "source_profile": str(args.camera_profile),
            "width": width,
            "height": height,
            "fx": float(new_k[0, 0]),
            "fy": float(new_k[1, 1]),
            "cx": float(new_k[0, 2]),
            "cy": float(new_k[1, 2]),
            "distortion_model": "pinhole_rectified",
            "distortion": [0.0, 0.0, 0.0, 0.0],
            "source_K": K.tolist(),
            "source_distortion": D.tolist(),
            "rectification_roi": [int(v) for v in roi],
        }

    stride = max(1, math.ceil(max(total_frames, 1) / max(args.max_candidates, 1)))
    print(f"Video (coded orientation): {width}x{height} @ {fps:.3f} FPS, {total_frames} frames")
    print(f"Candidate stride: {stride} (target <= {args.max_candidates})")
    print(f"Target selected views: {args.target_views}")
    print("Calibration:", "known + rectified" if args.camera_profile else "unknown (no undistortion)")

    candidates: list[Candidate] = []
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % stride == 0:
            metric_frame = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR) if map1 is not None else frame
            blur, mean, dark, bright, score, descriptor = metrics(metric_frame)
            candidates.append(
                Candidate(
                    frame_index=frame_idx,
                    timestamp=frame_idx / fps,
                    blur=blur,
                    mean_intensity=mean,
                    dark_clip=dark,
                    bright_clip=bright,
                    score=score,
                    descriptor=descriptor,
                )
            )
        frame_idx += 1
    cap.release()

    selected = select_candidates(
        candidates,
        args.target_views,
        args.min_blur,
        args.max_clip_fraction,
        args.duplicate_distance,
    )
    records = extract_selected(args.video, selected, image_dir, map1, map2)

    report = {
        "video": str(args.video),
        "fps": fps,
        "width": width,
        "height": height,
        "total_frames": total_frames,
        "candidate_stride": stride,
        "candidate_count": len(candidates),
        "selected_count": len(records),
        "target_views": args.target_views,
        "min_blur": args.min_blur,
        "duplicate_distance": args.duplicate_distance,
        "camera": camera_report,
        "frames": records,
    }
    (args.output / "manifest.json").write_text(json.dumps(report, indent=2))
    with (args.output / "camera.yaml").open("w") as handle:
        yaml.safe_dump(camera_report, handle, sort_keys=False)

    print("=== FRAME PREPARATION COMPLETE ===")
    print(f"Candidates: {len(candidates)}")
    print(f"Selected:   {len(records)}")
    print(f"Images:     {image_dir}")
    print(f"Manifest:   {args.output / 'manifest.json'}")


if __name__ == "__main__":
    main()
