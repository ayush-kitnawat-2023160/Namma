#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap
import yaml
from hloc import extract_features, match_features, triangulation
from scipy.spatial.transform import Rotation


def image_descriptor(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f"Cannot read {path}")
    small = cv2.resize(image, (32, 18), interpolation=cv2.INTER_AREA).astype(np.float32)
    small = (small - small.mean()) / (small.std() + 1e-6)
    return small.reshape(-1)


def build_pairs(names: list[str], image_dir: Path, temporal_window: int, visual_neighbors: int):
    pairs: set[tuple[str, str]] = set()
    n = len(names)
    for i in range(n):
        for j in range(i + 1, min(n, i + temporal_window + 1)):
            pairs.add((names[i], names[j]))

    if visual_neighbors > 0 and n > temporal_window + 2:
        descriptors = np.stack([image_descriptor(image_dir / name) for name in names])
        descriptors /= np.linalg.norm(descriptors, axis=1, keepdims=True) + 1e-8
        similarity = descriptors @ descriptors.T
        np.fill_diagonal(similarity, -np.inf)
        for i in range(n):
            for offset in range(-temporal_window, temporal_window + 1):
                j = i + offset
                if 0 <= j < n:
                    similarity[i, j] = -np.inf
            for j in np.argsort(similarity[i])[::-1][:visual_neighbors]:
                if not np.isfinite(similarity[i, j]):
                    continue
                a, b = sorted((i, int(j)))
                pairs.add((names[a], names[b]))
    return sorted(pairs)


def read_camera(path: Path):
    if not path.is_file():
        return None
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    return data if data.get("known_calibration") else None


def rotation_to_colmap_qvec(rotation: np.ndarray) -> np.ndarray:
    """Return COLMAP quaternion order [qw, qx, qy, qz] from a rotation matrix."""
    # Project tiny numerical errors back onto SO(3) before conversion.
    u, _, vt = np.linalg.svd(rotation)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u[:, -1] *= -1
        rotation = u @ vt
    qx, qy, qz, qw = Rotation.from_matrix(rotation).as_quat()
    return np.array([qw, qx, qy, qz], dtype=np.float64)


def make_vggt_reference_model(
    vggt_file: Path,
    image_names: list[str],
    known_camera: dict | None,
    output: Path,
) -> pycolmap.Reconstruction:
    data = np.load(vggt_file)
    vggt_names = [str(value) for value in data["image_names"].tolist()]
    if vggt_names != image_names:
        raise RuntimeError(
            "VGGT image order/names do not match selected SfM images. "
            "Re-run the VGGT stage after changing frame selection."
        )

    extrinsics = np.asarray(data["extrinsic"], dtype=np.float64)
    intrinsics = np.asarray(data["intrinsic"], dtype=np.float64)
    model_w = int(np.asarray(data["model_width"]).item())
    model_h = int(np.asarray(data["model_height"]).item())
    original_w = int(np.asarray(data["original_width"]).item())
    original_h = int(np.asarray(data["original_height"]).item())
    if len(extrinsics) != len(image_names):
        raise RuntimeError("VGGT camera count does not match image count.")

    output.mkdir(parents=True, exist_ok=True)
    cameras_txt = output / "cameras.txt"
    images_txt = output / "images.txt"
    points_txt = output / "points3D.txt"

    if known_camera:
        camera_params = np.array(
            [
                float(known_camera["fx"]),
                float(known_camera["fy"]),
                float(known_camera["cx"]),
                float(known_camera["cy"]),
            ],
            dtype=np.float64,
        )
        intrinsic_source = "known_rectified"
    else:
        sx = original_w / model_w
        sy = original_h / model_h
        scaled = intrinsics.copy()
        scaled[:, 0, 0] *= sx
        scaled[:, 0, 2] *= sx
        scaled[:, 1, 1] *= sy
        scaled[:, 1, 2] *= sy
        # A video comes from one physical camera. Robustly share one camera
        # instead of letting frame-to-frame network noise create N intrinsics.
        camera_params = np.array(
            [
                np.median(scaled[:, 0, 0]),
                np.median(scaled[:, 1, 1]),
                np.median(scaled[:, 0, 2]),
                np.median(scaled[:, 1, 2]),
            ],
            dtype=np.float64,
        )
        intrinsic_source = "vggt_median_shared"

    cameras_txt.write_text(
        "# Camera list with one line of data per camera:\n"
        "# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"
        f"1 PINHOLE {original_w} {original_h} "
        + " ".join(f"{value:.12g}" for value in camera_params)
        + "\n"
    )

    with images_txt.open("w") as handle:
        handle.write("# VGGT-Omega initialized image poses\n")
        handle.write("# IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME\n")
        for image_id, (name, ext) in enumerate(zip(image_names, extrinsics), start=1):
            qvec = rotation_to_colmap_qvec(ext[:3, :3])
            tvec = ext[:3, 3]
            values = [*qvec.tolist(), *tvec.tolist()]
            handle.write(
                f"{image_id} "
                + " ".join(f"{value:.17g}" for value in values)
                + f" 1 {name}\n\n"
            )
    points_txt.write_text("# Empty: LightGlue tracks are triangulated in the next stage.\n")

    reference = pycolmap.Reconstruction(str(output))
    if reference.num_reg_images() != len(image_names):
        raise RuntimeError(
            f"VGGT reference model loaded {reference.num_reg_images()}/{len(image_names)} poses."
        )
    metadata = {
        "images": len(image_names),
        "camera_model": "PINHOLE",
        "camera_params": camera_params.tolist(),
        "intrinsic_source": intrinsic_source,
        "model_resolution": [model_w, model_h],
        "original_resolution": [original_w, original_h],
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return reference


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--camera", type=Path)
    parser.add_argument("--vggt", type=Path, required=True)
    parser.add_argument("--temporal-window", type=int, default=5)
    parser.add_argument("--visual-neighbors", type=int, default=5)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    names = sorted(
        p.name for p in args.images.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if len(names) < 3:
        raise RuntimeError("Need at least three selected views for SfM.")

    pairs = build_pairs(names, args.images, args.temporal_window, args.visual_neighbors)
    pairs_path = args.output / "pairs.txt"
    pairs_path.write_text("".join(f"{a} {b}\n" for a, b in pairs))
    print(f"Pair graph: {len(names)} images, {len(pairs)} candidate pairs")

    features = args.output / "features.h5"
    matches = args.output / "matches.h5"
    feature_conf = extract_features.confs["superpoint_max"]
    matcher_conf = match_features.confs["superpoint+lightglue"]

    extract_features.main(
        feature_conf,
        args.images,
        image_list=names,
        feature_path=features,
    )
    match_features.main(
        matcher_conf,
        pairs_path,
        features=features,
        matches=matches,
    )

    known = read_camera(args.camera) if args.camera else None
    reference_dir = args.output / "vggt_reference"
    make_vggt_reference_model(args.vggt, names, known, reference_dir)
    print(
        "Camera initialization:",
        "known rectified K + VGGT poses" if known else "VGGT shared K + VGGT poses",
    )

    model_dir = args.output / "model"
    mapper_options = {
        "ba_refine_focal_length": not bool(known),
        "ba_refine_principal_point": False,
        "ba_refine_extra_params": False,
        "mapper": {
            "abs_pose_refine_focal_length": not bool(known),
            "abs_pose_refine_extra_params": False,
        },
    }
    model = triangulation.main(
        model_dir,
        reference_dir,
        args.images,
        pairs_path,
        features,
        matches,
        estimate_two_view_geometries=True,
        mapper_options=mapper_options,
        verbose=True,
    )

    ba_options = pycolmap.BundleAdjustmentOptions()
    ba_options.print_summary = True
    if known:
        ba_options.refine_focal_length = False
        ba_options.refine_principal_point = False
        ba_options.refine_extra_params = False
    pycolmap.bundle_adjustment(model, ba_options)
    model.write(model_dir)

    num_registered = int(model.num_reg_images())
    result = {
        "input_images": len(names),
        "registered_images": num_registered,
        "registration_fraction": num_registered / len(names),
        "pairs": len(pairs),
        "points3D": int(model.num_points3D()),
        "known_intrinsics": bool(known),
        "pose_initialization": "VGGT-Omega",
        "triangulation": "SuperPoint + LightGlue",
        "bundle_adjustment": "pycolmap global BA",
    }
    (args.output / "summary.json").write_text(json.dumps(result, indent=2))
    print("=== LIGHTGLUE + COLMAP BA COMPLETE ===")
    print(model.summary())
    if num_registered < 0.8 * len(names):
        print(f"[WARN] Only {num_registered}/{len(names)} selected views registered.")


if __name__ == "__main__":
    main()
