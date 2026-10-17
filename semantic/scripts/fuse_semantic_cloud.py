#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pycolmap
import yaml
from plyfile import PlyData, PlyElement
from scipy.optimize import linear_sum_assignment


NEXTGEN_ROOT = Path("/workspace/nextgen")
SEMANTIC_ROOT = Path("/workspace/semantic")
DEFAULT_CONFIG = Path("/opt/namma-semantic/config/default.yaml")

SEMANTIC_RGB = {
    "floor": (70, 180, 80),
    "wall": (230, 130, 55),
    "ceiling": (190, 190, 190),
    "pillar": (185, 85, 205),
    "door": (50, 170, 225),
    "chair": (235, 95, 55),
    "table": (230, 185, 45),
    "sofa": (205, 75, 155),
    "cabinet": (85, 205, 205),
}


@dataclass
class Fragment:
    fragment_id: int
    image: str
    frame_index: int
    local_id: int
    class_name: str
    detector_score: float
    point_ids: np.ndarray
    centroid: np.ndarray
    radius: float


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> dict:
    with DEFAULT_CONFIG.open() as handle:
        return yaml.safe_load(handle) or {}


def read_dense_ply(path: Path) -> tuple[np.ndarray, np.ndarray]:
    if not path.is_file():
        raise RuntimeError(f"Missing BA-aligned dense VGGT cloud: {path}")
    ply = PlyData.read(path)
    vertex = ply["vertex"]
    xyz = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=1).astype(np.float32)
    names = set(vertex.data.dtype.names or [])
    if {"red", "green", "blue"}.issubset(names):
        rgb = np.stack([vertex["red"], vertex["green"], vertex["blue"]], axis=1).astype(np.uint8)
    else:
        rgb = np.full((len(xyz), 3), 180, dtype=np.uint8)
    return xyz, rgb


def write_rgb_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
    ]
    output = np.empty(len(xyz), dtype=dtype)
    output["x"], output["y"], output["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    output["red"], output["green"], output["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    PlyData([PlyElement.describe(output, "vertex")], text=False).write(path)


def write_semantic_ply(
    path: Path,
    xyz: np.ndarray,
    rgb: np.ndarray,
    semantic_ids: np.ndarray,
    confidence: np.ndarray,
    votes: np.ndarray,
    observations: np.ndarray,
    source_indices: np.ndarray,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
        ("semantic_id", "u1"),
        ("semantic_confidence", "f4"),
        ("semantic_votes", "u2"),
        ("observations", "u2"),
        ("source_index", "u4"),
    ]
    output = np.empty(len(xyz), dtype=dtype)
    output["x"], output["y"], output["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    output["red"], output["green"], output["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    output["semantic_id"] = semantic_ids.astype(np.uint8)
    output["semantic_confidence"] = confidence.astype(np.float32)
    output["semantic_votes"] = votes.astype(np.uint16)
    output["observations"] = observations.astype(np.uint16)
    output["source_index"] = source_indices.astype(np.uint32)
    PlyData([PlyElement.describe(output, "vertex")], text=False).write(path)


def camera_pose(image) -> tuple[np.ndarray, np.ndarray]:
    pose = image.cam_from_world()
    matrix = np.asarray(pose.matrix(), dtype=np.float64)
    if matrix.shape != (3, 4):
        raise RuntimeError(f"Unexpected COLMAP pose shape for {image.name}: {matrix.shape}")
    return matrix[:, :3], matrix[:, 3]


def pinhole_params(camera, width: int, height: int) -> tuple[float, float, float, float]:
    model = str(camera.model_name)
    params = np.asarray(camera.params, dtype=np.float64)
    if model == "PINHOLE":
        fx, fy, cx, cy = params[:4]
    elif model == "SIMPLE_PINHOLE":
        fx, cx, cy = params[:3]
        fy = fx
    else:
        raise RuntimeError(
            f"Stage 2 currently expects the rectified PINHOLE camera used by NextGen; got {model}."
        )
    sx = width / float(camera.width)
    sy = height / float(camera.height)
    return float(fx * sx), float(fy * sy), float(cx * sx), float(cy * sy)


def visible_projection(
    xyz: np.ndarray,
    image,
    width: int,
    height: int,
    z_tolerance: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    R, t = camera_pose(image)
    camera_xyz = xyz.astype(np.float64) @ R.T + t
    z = camera_xyz[:, 2]
    valid = z > 1e-6
    point_ids = np.flatnonzero(valid)
    camera_xyz = camera_xyz[valid]
    z = z[valid]

    fx, fy, cx, cy = pinhole_params(image.camera, width, height)
    u = np.rint(fx * camera_xyz[:, 0] / z + cx).astype(np.int32)
    v = np.rint(fy * camera_xyz[:, 1] / z + cy).astype(np.int32)
    inside = (u >= 0) & (u < width) & (v >= 0) & (v < height)
    point_ids, u, v, z = point_ids[inside], u[inside], v[inside], z[inside]
    if len(point_ids) == 0:
        return point_ids, u, v

    pixel = v.astype(np.int64) * width + u.astype(np.int64)
    depth_buffer = np.full(height * width, np.inf, dtype=np.float64)
    np.minimum.at(depth_buffer, pixel, z)
    nearest = depth_buffer[pixel]
    tolerance = np.maximum(float(z_tolerance), 0.002 * nearest)
    visible = z <= nearest + tolerance
    return point_ids[visible], u[visible], v[visible]


def load_frame_instances(path: Path) -> dict[int, dict]:
    record = json.loads(path.read_text())
    return {int(item["local_id"]): item for item in record.get("instances", [])}


def make_fragment(
    fragment_id: int,
    image_name: str,
    frame_index: int,
    item: dict,
    point_ids: np.ndarray,
    xyz: np.ndarray,
) -> Fragment:
    point_ids = np.unique(point_ids.astype(np.int64))
    pts = xyz[point_ids]
    centroid = pts.mean(axis=0).astype(np.float64)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    radius = float(0.5 * np.linalg.norm(hi - lo))
    return Fragment(
        fragment_id=fragment_id,
        image=image_name,
        frame_index=frame_index,
        local_id=int(item["local_id"]),
        class_name=str(item["class"]),
        detector_score=float(item.get("score", 0.0)),
        point_ids=point_ids,
        centroid=centroid,
        radius=radius,
    )


def associate_fragments(
    fragments: list[Fragment],
    overlap_threshold: float,
    min_shared: int,
    max_distance: float,
    max_frame_gap: int,
) -> tuple[list[list[int]], int, int]:
    """Associate frame-local fragments into persistent tracks.

    The old implementation built a pairwise overlap graph and returned its
    connected components. That allows transitive bridges: A~B and B~C can
    merge A and C even when they are two different objects. Here we process
    detections in frame order and solve a one-to-one bipartite assignment per
    class per frame. Therefore a track can never absorb two detections from the
    same frame, which is a strong cannot-link constraint for static scenes.
    """
    if not fragments:
        return [], 0, 0

    by_frame_class: dict[tuple[int, str], list[int]] = defaultdict(list)
    for index, fragment in enumerate(fragments):
        by_frame_class[(fragment.frame_index, fragment.class_name)].append(index)

    tracks_by_class: dict[str, list[dict]] = defaultdict(list)
    accepted_links = 0
    rejected_same_frame = 0

    for (frame_index, class_name), current_indices in sorted(by_frame_class.items()):
        tracks = tracks_by_class[class_name]
        active_indices = [
            index
            for index, track in enumerate(tracks)
            if frame_index - track["last_frame"] <= max_frame_gap
        ]

        if active_indices and current_indices:
            scores = np.full(
                (len(current_indices), len(active_indices)),
                -1e6,
                dtype=np.float64,
            )
            for row, fragment_index in enumerate(current_indices):
                fragment = fragments[fragment_index]
                for col, track_index in enumerate(active_indices):
                    track = tracks[track_index]
                    if frame_index in track["frames"]:
                        rejected_same_frame += 1
                        continue
                    shared = int(
                        np.intersect1d(
                            fragment.point_ids,
                            track["point_ids"],
                            assume_unique=True,
                        ).size
                    )
                    if shared < min_shared:
                        continue
                    # Measure how much of the current observation is already
                    # explained by this track. Using the ever-growing track
                    # union as the denominator would make later matches harder.
                    overlap = shared / max(1, len(fragment.point_ids))
                    if overlap < overlap_threshold:
                        continue
                    distance = float(
                        np.linalg.norm(fragment.centroid - track["centroid"])
                    )
                    allowed = max(
                        float(max_distance),
                        0.75 * (fragment.radius + track["radius"]),
                    )
                    if distance > allowed:
                        continue
                    # Overlap is dominant; shared support and spatial
                    # consistency only break ties between plausible matches.
                    support = min(1.0, shared / max(1.0, 4.0 * min_shared))
                    spatial = max(0.0, 1.0 - distance / max(allowed, 1e-6))
                    scores[row, col] = overlap + 0.10 * support + 0.05 * spatial

            rows, cols = linear_sum_assignment(-scores)
            assigned_fragments: set[int] = set()
            for row, col in zip(rows.tolist(), cols.tolist()):
                if scores[row, col] <= -1e5:
                    continue
                fragment_index = current_indices[row]
                track_index = active_indices[col]
                fragment = fragments[fragment_index]
                track = tracks[track_index]
                track["fragment_indices"].append(fragment_index)
                track["frames"].add(frame_index)
                track["last_frame"] = frame_index
                track["point_ids"] = np.union1d(
                    track["point_ids"], fragment.point_ids
                )
                # Recompute geometry from the constituent fragment summaries.
                members = [fragments[idx] for idx in track["fragment_indices"]]
                weights = np.asarray([len(member.point_ids) for member in members], dtype=np.float64)
                centroids = np.stack([member.centroid for member in members])
                track["centroid"] = np.average(centroids, axis=0, weights=weights)
                track["radius"] = max(
                    float(np.linalg.norm(member.centroid - track["centroid"])) + member.radius
                    for member in members
                )
                assigned_fragments.add(fragment_index)
                accepted_links += 1
        else:
            assigned_fragments = set()

        for fragment_index in current_indices:
            if fragment_index in assigned_fragments:
                continue
            fragment = fragments[fragment_index]
            tracks.append(
                {
                    "fragment_indices": [fragment_index],
                    "frames": {frame_index},
                    "last_frame": frame_index,
                    "point_ids": fragment.point_ids.copy(),
                    "centroid": fragment.centroid.copy(),
                    "radius": float(fragment.radius),
                }
            )

    groups = [
        track["fragment_indices"]
        for tracks in tracks_by_class.values()
        for track in tracks
    ]
    return groups, accepted_links, rejected_same_frame


def main() -> None:
    parser = argparse.ArgumentParser(description="Namma Stage 2: semantic 3D fusion")
    parser.add_argument("scene")
    parser.add_argument("--overwrite", action="store_true", default=env_bool("NS_FUSION_OVERWRITE"))
    args = parser.parse_args()

    config = load_config()
    fusion = config["fusion"]
    z_tolerance = float(os.environ.get("NS_FUSION_Z_TOLERANCE", fusion["z_buffer_tolerance"]))
    min_votes = int(os.environ.get("NS_FUSION_MIN_VOTES", fusion["min_semantic_votes"]))
    min_ratio = float(os.environ.get("NS_FUSION_MIN_RATIO", fusion["min_vote_ratio"]))
    min_fragment_points = int(
        os.environ.get("NS_FUSION_MIN_FRAGMENT_POINTS", fusion["min_fragment_points"])
    )
    merge_overlap = float(
        os.environ.get("NS_FUSION_INSTANCE_OVERLAP", fusion["instance_merge_overlap"])
    )
    merge_min_shared = int(
        os.environ.get("NS_FUSION_INSTANCE_MIN_SHARED", fusion["instance_merge_min_shared_points"])
    )
    merge_distance = float(
        os.environ.get("NS_FUSION_INSTANCE_DISTANCE", fusion["instance_merge_distance"])
    )
    merge_max_gap = int(
        os.environ.get("NS_FUSION_INSTANCE_MAX_GAP", fusion["instance_max_frame_gap"])
    )
    min_track_views = int(os.environ.get("NS_FUSION_MIN_TRACK_VIEWS", fusion["min_track_views"]))
    min_instance_points = int(
        os.environ.get("NS_FUSION_MIN_INSTANCE_POINTS", fusion["min_instance_points"])
    )
    min_object_point_votes = int(
        os.environ.get("NS_FUSION_MIN_OBJECT_POINT_VOTES", fusion["min_object_point_votes"])
    )

    semantic_base = SEMANTIC_ROOT / args.scene
    stage1 = semantic_base / "01_segmentation"
    stage1_summary = stage1 / "summary.json"
    if not stage1_summary.is_file():
        raise RuntimeError(f"Missing Stage-1 summary: {stage1_summary}")
    segmentation_summary = json.loads(stage1_summary.read_text())
    if not segmentation_summary.get("complete_dataset"):
        raise RuntimeError("Stage 2 requires a complete Stage-1 run, not a preview.")

    labels_file = stage1 / "labels.json"
    labels = json.loads(labels_file.read_text())
    structural_ids = {
        str(name): int(value)
        for name, value in labels["semantic_ids"].items()
        if str(name) != "background"
    }
    object_classes = [str(value) for value in labels.get("object_classes", [])]
    ordered_classes = list(structural_ids.keys()) + [
        value for value in object_classes if value not in structural_ids
    ]
    class_to_id = {name: index + 1 for index, name in enumerate(ordered_classes)}
    id_to_class = {value: key for key, value in class_to_id.items()}

    nextgen = NEXTGEN_ROOT / args.scene
    aligned_cloud = nextgen / "04_gs_dataset/sparse/0/points3D.ply"
    sfm_model = nextgen / "03_sfm/model"
    frames_dir = nextgen / "01_frames/images"
    output = semantic_base / "02_semantic_cloud"
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Stage-2 output already exists: {output}. Use --overwrite to replace it.")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    print("=== NAMMA STAGE 2: SEMANTIC 3D FUSION ===")
    print(f"Scene:               {args.scene}")
    print(f"Dense source:        {aligned_cloud}")
    print(f"SfM cameras:         {sfm_model}")
    print(f"Z tolerance:         {z_tolerance}")
    print(f"Min semantic votes:  {min_votes}")
    print(f"Min vote ratio:      {min_ratio}")

    xyz, rgb = read_dense_ply(aligned_cloud)
    point_count = len(xyz)
    reconstruction = pycolmap.Reconstruction(str(sfm_model))
    images_by_name = {image.name: image for image in reconstruction.images.values()}
    frame_paths = sorted(
        path for path in frames_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if len(frame_paths) != int(segmentation_summary["images_processed"]):
        raise RuntimeError(
            f"Frame count changed after Stage 1: {len(frame_paths)} vs "
            f"{segmentation_summary['images_processed']}."
        )

    class_count = len(class_to_id) + 1
    votes = np.zeros((point_count, class_count), dtype=np.uint16)
    observations = np.zeros(point_count, dtype=np.uint16)
    fragments: list[Fragment] = []
    visible_per_frame: list[int] = []

    for frame_index, frame in enumerate(frame_paths):
        if frame.name not in images_by_name:
            raise RuntimeError(f"No BA camera found for {frame.name}")
        semantic = cv2.imread(str(stage1 / "semantic" / f"{frame.stem}.png"), cv2.IMREAD_UNCHANGED)
        instance_map = cv2.imread(
            str(stage1 / "instance_maps" / f"{frame.stem}.png"), cv2.IMREAD_UNCHANGED
        )
        if semantic is None or instance_map is None:
            raise RuntimeError(f"Missing Stage-1 maps for {frame.name}")
        height, width = semantic.shape[:2]
        if instance_map.shape[:2] != (height, width):
            raise RuntimeError(f"Semantic/instance map size mismatch for {frame.name}")

        point_ids, u, v = visible_projection(
            xyz,
            images_by_name[frame.name],
            width,
            height,
            z_tolerance,
        )
        visible_per_frame.append(len(point_ids))
        np.add.at(observations, point_ids, 1)

        structural = semantic[v, u].astype(np.int32)
        for name, stage1_id in structural_ids.items():
            mask = structural == stage1_id
            if np.any(mask):
                np.add.at(votes[:, class_to_id[name]], point_ids[mask], 1)

        instance_values = instance_map[v, u].astype(np.int32)
        instance_meta = load_frame_instances(stage1 / "instances" / f"{frame.stem}.json")
        for local_id, item in instance_meta.items():
            class_name = str(item["class"])
            if class_name not in class_to_id:
                continue
            mask = instance_values == int(local_id)
            if not np.any(mask):
                continue
            ids = np.unique(point_ids[mask])
            if len(ids) < min_fragment_points:
                continue
            np.add.at(votes[:, class_to_id[class_name]], ids, 1)
            fragments.append(
                make_fragment(
                    len(fragments),
                    frame.name,
                    frame_index,
                    item,
                    ids,
                    xyz,
                )
            )

        if frame_index == 0 or (frame_index + 1) % 10 == 0 or frame_index + 1 == len(frame_paths):
            print(
                f"[fusion] {frame_index + 1}/{len(frame_paths)} {frame.name}: "
                f"{len(point_ids)} visible dense points, {len(fragments)} fragments total"
            )

    print(f"[instances] associating {len(fragments)} multi-view fragments...")
    groups, association_links, rejected_same_frame = associate_fragments(
        fragments,
        merge_overlap,
        merge_min_shared,
        merge_distance,
        merge_max_gap,
    )

    track_candidates = []
    for group in groups:
        class_names = {fragments[index].class_name for index in group}
        if len(class_names) != 1:
            continue
        class_name = next(iter(class_names))
        point_ids = np.unique(
            np.concatenate([fragments[index].point_ids for index in group])
        )
        images = sorted({fragments[index].image for index in group})
        pts = xyz[point_ids]
        track_candidates.append(
            {
                "class": class_name,
                "fragment_indices": group,
                "point_ids": point_ids,
                "views": images,
                "point_count": len(point_ids),
                "centroid": pts.mean(axis=0).astype(float).tolist(),
                "bbox_min": pts.min(axis=0).astype(float).tolist(),
                "bbox_max": pts.max(axis=0).astype(float).tolist(),
                "mean_detector_score": float(
                    np.mean([fragments[index].detector_score for index in group])
                ),
            }
        )

    # Deterministic persistent IDs. Exported tracks are also used to override
    # structural labels (e.g. chair points that a stuff model called floor).
    track_candidates.sort(key=lambda item: (item["class"], *item["centroid"]))
    class_counters = Counter()
    exported_tracks = []
    object_override_id = np.zeros(point_count, dtype=np.uint8)
    object_override_score = np.zeros(point_count, dtype=np.uint16)
    instances_dir = output / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)

    for track in track_candidates:
        class_counters[track["class"]] += 1
        persistent_id = f"{track['class']}_{class_counters[track['class']]:03d}"
        exported = (
            len(track["views"]) >= min_track_views
            and int(track["point_count"]) >= min_instance_points
        )
        filename = f"{persistent_id}.ply" if exported else None
        if exported:
            raw_ids = track["point_ids"]
            class_id = class_to_id[track["class"]]
            supported = votes[raw_ids, class_id] >= min_object_point_votes
            ids = raw_ids[supported]
            # Keep a usable object if the conservative multi-view filter is too
            # harsh for a thin/partially observed surface, but record this in
            # the track diagnostics.
            used_vote_filter = len(ids) >= min_instance_points
            if not used_vote_filter:
                ids = raw_ids
            write_rgb_ply(instances_dir / filename, xyz[ids], rgb[ids])
            score = min(65535, len(track["views"]))
            replace = score > object_override_score[ids]
            chosen = ids[replace]
            object_override_id[chosen] = class_to_id[track["class"]]
            object_override_score[chosen] = score
            exported_tracks.append(persistent_id)
            track["exported_point_count"] = int(len(ids))
            track["used_object_vote_filter"] = bool(used_vote_filter)
        track["persistent_id"] = persistent_id
        track["exported"] = exported
        track["filename"] = filename
        track["fragment_ids"] = [fragments[index].fragment_id for index in track["fragment_indices"]]
        del track["fragment_indices"]
        del track["point_ids"]

    # Structural multi-view voting first, then high-confidence persistent object
    # tracks override the stuff result.
    structural_class_ids = [class_to_id[name] for name in structural_ids]
    structural_votes = votes[:, structural_class_ids]
    structural_sum = structural_votes.sum(axis=1).astype(np.float32)
    structural_best_offset = np.argmax(structural_votes, axis=1)
    structural_best_votes = structural_votes[
        np.arange(point_count), structural_best_offset
    ].astype(np.uint16)
    structural_best_id = np.asarray(structural_class_ids, dtype=np.uint8)[structural_best_offset]
    structural_ratio = structural_best_votes.astype(np.float32) / np.maximum(structural_sum, 1.0)

    final_id = np.zeros(point_count, dtype=np.uint8)
    final_confidence = np.zeros(point_count, dtype=np.float32)
    final_votes = np.zeros(point_count, dtype=np.uint16)
    structural_ok = (
        (structural_best_votes >= min_votes)
        & (structural_ratio >= min_ratio)
    )
    final_id[structural_ok] = structural_best_id[structural_ok]
    final_confidence[structural_ok] = structural_ratio[structural_ok]
    final_votes[structural_ok] = structural_best_votes[structural_ok]

    object_mask = object_override_id > 0
    final_id[object_mask] = object_override_id[object_mask]
    # Track view count is a stronger cue than a single stuff vote; confidence is
    # capped at one for convenient downstream interpretation.
    final_confidence[object_mask] = np.minimum(
        1.0, object_override_score[object_mask].astype(np.float32) / max(2, min_track_views)
    )
    final_votes[object_mask] = object_override_score[object_mask]

    labeled = final_id > 0
    source_indices = np.flatnonzero(labeled).astype(np.uint32)
    semantic_rgb = np.zeros((int(np.count_nonzero(labeled)), 3), dtype=np.uint8)
    for class_id, class_name in id_to_class.items():
        mask = final_id[labeled] == class_id
        semantic_rgb[mask] = np.asarray(SEMANTIC_RGB.get(class_name, (255, 255, 255)), dtype=np.uint8)

    write_semantic_ply(
        output / "semantic_scene.ply",
        xyz[labeled],
        semantic_rgb,
        final_id[labeled],
        final_confidence[labeled],
        final_votes[labeled],
        observations[labeled],
        source_indices,
    )
    write_semantic_ply(
        output / "semantic_scene_rgb.ply",
        xyz[labeled],
        rgb[labeled],
        final_id[labeled],
        final_confidence[labeled],
        final_votes[labeled],
        observations[labeled],
        source_indices,
    )

    classes_dir = output / "classes"
    class_point_counts = {}
    for class_id, class_name in id_to_class.items():
        ids = np.flatnonzero(final_id == class_id)
        class_point_counts[class_name] = int(len(ids))
        if len(ids):
            write_rgb_ply(classes_dir / f"{class_name}.ply", xyz[ids], rgb[ids])

    fragment_records = [
        {
            "fragment_id": fragment.fragment_id,
            "image": fragment.image,
            "frame_index": fragment.frame_index,
            "local_id": fragment.local_id,
            "class": fragment.class_name,
            "detector_score": fragment.detector_score,
            "point_count": int(len(fragment.point_ids)),
            "centroid": fragment.centroid.astype(float).tolist(),
            "radius": fragment.radius,
        }
        for fragment in fragments
    ]
    (output / "fragments.json").write_text(json.dumps(fragment_records, indent=2))
    (output / "instance_tracks.json").write_text(json.dumps(track_candidates, indent=2))
    (output / "labels.json").write_text(
        json.dumps(
            {
                "class_to_id": class_to_id,
                "id_to_class": {str(key): value for key, value in id_to_class.items()},
                "semantic_palette_rgb": SEMANTIC_RGB,
            },
            indent=2,
        )
    )

    track_counts = Counter(
        track["class"] for track in track_candidates if track["exported"]
    )
    summary = {
        "scene": args.scene,
        "complete": True,
        "source_dense_points": point_count,
        "labeled_points": int(np.count_nonzero(labeled)),
        "labeled_fraction": float(np.count_nonzero(labeled) / max(1, point_count)),
        "class_point_counts": class_point_counts,
        "frames": len(frame_paths),
        "mean_visible_dense_points_per_frame": float(np.mean(visible_per_frame)),
        "fragments": len(fragments),
        "association_links": association_links,
        "same_frame_links_rejected": rejected_same_frame,
        "track_candidates": len(track_candidates),
        "exported_instances": len(exported_tracks),
        "exported_instances_by_class": dict(track_counts),
        "thresholds": {
            "z_buffer_tolerance": z_tolerance,
            "min_semantic_votes": min_votes,
            "min_vote_ratio": min_ratio,
            "min_fragment_points": min_fragment_points,
            "instance_merge_overlap": merge_overlap,
            "instance_merge_min_shared_points": merge_min_shared,
            "instance_merge_distance": merge_distance,
            "instance_max_frame_gap": merge_max_gap,
            "min_track_views": min_track_views,
            "min_instance_points": min_instance_points,
            "min_object_point_votes": min_object_point_votes,
        },
        "association_method": "frame-aware one-to-one overlap tracking",
        "geometry_source": "BA-aligned confidence-filtered VGGT dense cloud",
        "camera_source": "COLMAP global-BA model",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    (output / ".complete").write_text("ok\n")

    print("=== STAGE 2 COMPLETE ===")
    print(json.dumps(summary, indent=2))
    print(f"Semantic cloud: {output / 'semantic_scene.ply'}")
    print(f"Persistent instances: {instances_dir}")


if __name__ == "__main__":
    main()
