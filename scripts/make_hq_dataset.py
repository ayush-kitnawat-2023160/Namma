#!/usr/bin/env python3

import argparse
import shutil
from pathlib import Path

import cv2
import numpy as np
import yaml


def get_colmap_image_names(images_txt, image_dir):
    valid_names = {
        p.name for p in Path(image_dir).iterdir()
        if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
    }

    records = []

    for line in Path(images_txt).read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        parts = line.split()

        # COLMAP image header:
        # IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME
        if len(parts) >= 10 and parts[-1] in valid_names:
            try:
                image_id = int(parts[0])
            except ValueError:
                continue

            records.append((image_id, parts[-1]))

    records.sort(key=lambda x: x[0])
    return [name for _, name in records]


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--run", required=True)
    ap.add_argument("--out-run", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--camera", required=True)

    args = ap.parse_args()

    src = Path("/workspace/outputs/gs_dataset") / args.run
    dst = Path("/workspace/outputs/gs_dataset") / args.out_run

    traj_path = (
        Path("/workspace/outputs/mast3r")
        / args.run
        / "frames.txt"
    )

    if not src.exists():
        raise RuntimeError(f"Missing source dataset: {src}")

    if not traj_path.exists():
        raise RuntimeError(f"Missing trajectory: {traj_path}")

    if dst.exists():
        raise RuntimeError(
            f"{dst} already exists. Delete it first if you want to recreate it."
        )

    print(f"Copying:\n  {src}\n-> {dst}")
    shutil.copytree(src, dst)

    # --------------------------------------------------------
    # Camera calibration
    # --------------------------------------------------------

    with open(args.camera) as f:
        c = yaml.safe_load(f)

    ref_w = int(c["width"])
    ref_h = int(c["height"])

    K = np.array([
        [float(c["fx"]), 0.0, float(c["cx"])],
        [0.0, float(c["fy"]), float(c["cy"])],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)

    D = np.asarray(
        c.get("distortion", [0, 0, 0, 0]),
        dtype=np.float64
    )

    # --------------------------------------------------------
    # Trajectory timestamps
    # --------------------------------------------------------

    traj = np.loadtxt(traj_path)

    if traj.ndim == 1:
        traj = traj[None, :]

    timestamps = traj[:, 0]

    # --------------------------------------------------------
    # Exact image names expected by COLMAP
    # --------------------------------------------------------

    names = get_colmap_image_names(
        src / "sparse/0/images.txt",
        src / "images",
    )

    print("Trajectory poses:", len(timestamps))
    print("COLMAP images:    ", len(names))

    if len(timestamps) != len(names):
        raise RuntimeError(
            f"Mismatch: {len(timestamps)} timestamps "
            f"vs {len(names)} COLMAP images"
        )

    # --------------------------------------------------------
    # Video
    # --------------------------------------------------------

    cap = cv2.VideoCapture(args.video)

    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {args.video}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"Video: {W}x{H} @ {fps:.6f} FPS")
    print(f"Calibration reference: {ref_w}x{ref_h}")

    if (W, H) != (ref_w, ref_h):
        raise RuntimeError(
            f"Video is {W}x{H}, but calibration is {ref_w}x{ref_h}. "
            "Do not continue until orientation/resolution is checked."
        )

    # --------------------------------------------------------
    # Undistorted pinhole K
    # --------------------------------------------------------

    newK, roi = cv2.getOptimalNewCameraMatrix(
        K,
        D,
        (W, H),
        0,
        (W, H)
    )

    map1, map2 = cv2.initUndistortRectifyMap(
        K,
        D,
        None,
        newK,
        (W, H),
        cv2.CV_32FC1
    )

    print("\nOriginal K:")
    print(K)

    print("\nUndistorted K:")
    print(newK)

    print("\nROI:", roi)

    # --------------------------------------------------------
    # Replace low-resolution image directory
    # --------------------------------------------------------

    image_dir = dst / "images"
    shutil.rmtree(image_dir)
    image_dir.mkdir(parents=True)

    # --------------------------------------------------------
    # Extract EXACT original video frame for every keyframe
    # --------------------------------------------------------

    for i, (timestamp, name) in enumerate(zip(timestamps, names)):

        frame_idx = int(round(float(timestamp) * fps))

        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()

        if not ok:
            raise RuntimeError(
                f"Could not read frame {frame_idx} "
                f"(timestamp={timestamp})"
            )

        frame = cv2.remap(
            frame,
            map1,
            map2,
            cv2.INTER_LINEAR
        )

        output = image_dir / name

        if not cv2.imwrite(str(output), frame):
            raise RuntimeError(f"Failed writing {output}")

        print(
            f"[{i+1:03d}/{len(names):03d}] "
            f"t={timestamp:.6f} "
            f"frame={frame_idx} "
            f"-> {name}"
        )

    cap.release()

    # --------------------------------------------------------
    # Update COLMAP camera
    # --------------------------------------------------------

    cameras_txt = dst / "sparse/0/cameras.txt"

    old_lines = cameras_txt.read_text().splitlines()

    comments = [
        line for line in old_lines
        if line.startswith("#")
    ]

    camera_line = (
        f"1 PINHOLE "
        f"{W} {H} "
        f"{newK[0,0]:.10f} "
        f"{newK[1,1]:.10f} "
        f"{newK[0,2]:.10f} "
        f"{newK[1,2]:.10f}"
    )

    cameras_txt.write_text(
        "\n".join(comments)
        + ("\n" if comments else "")
        + camera_line
        + "\n"
    )

    result = {
        "width": W,
        "height": H,
        "fx": float(newK[0,0]),
        "fy": float(newK[1,1]),
        "cx": float(newK[0,2]),
        "cy": float(newK[1,2]),
        "source_run": args.run,
        "video_fps": float(fps),
    }

    with open(dst / "hq_camera.yaml", "w") as f:
        yaml.safe_dump(result, f)

    print("\n==============================================")
    print(" HQ 3DGS DATASET COMPLETE")
    print("==============================================")
    print("Dataset:", dst)
    print("Images:", W, "x", H)
    print("Camera:")
    print(camera_line)


if __name__ == "__main__":
    main()
