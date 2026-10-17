#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
import pycolmap
import trimesh
import yaml


NEXTGEN_ROOT = Path("/workspace/nextgen")
SEMANTIC_ROOT = Path("/workspace/semantic")
DEFAULT_CONFIG = Path("/opt/namma-semantic/config/default.yaml")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> dict:
    with DEFAULT_CONFIG.open() as handle:
        return yaml.safe_load(handle) or {}


def load_track_points(path: Path) -> np.ndarray:
    cloud = o3d.io.read_point_cloud(str(path))
    return np.asarray(cloud.points, dtype=np.float32)


def quantized_keys(points: np.ndarray, voxel: float) -> np.ndarray:
    quantized = np.rint(points / max(float(voxel), 1e-8)).astype(np.int32)
    dtype = np.dtype([("x", np.int32), ("y", np.int32), ("z", np.int32)])
    return np.ascontiguousarray(quantized).view(dtype).reshape(-1)


def consolidate_tracks(
    tracks: list[dict],
    instances_dir: Path,
    overlap_threshold: float,
    duplicate_voxel: float,
    centroid_distance: float,
) -> list[dict]:
    """Merge only very strong duplicate tracks.

    Tracks that share a source frame are a hard cannot-link because the Stage-2
    one-to-one tracker already established them as simultaneous distinct
    detections. Non-overlapping-view tracks are merged only when most of the
    smaller 3D support cloud occupies the same quantized world points.
    """
    exported = [track for track in tracks if track.get("exported") and track.get("filename")]
    cache: dict[str, dict] = {}
    for track in exported:
        points = load_track_points(instances_dir / track["filename"])
        cache[track["persistent_id"]] = {
            "points": points,
            "keys": np.unique(quantized_keys(points, duplicate_voxel)),
        }

    groups: list[dict] = []
    for track in sorted(exported, key=lambda item: (item["class"], item["persistent_id"])):
        current = cache[track["persistent_id"]]
        best_index = None
        best_overlap = 0.0
        current_views = set(track.get("views", []))
        current_centroid = np.asarray(track["centroid"], dtype=np.float64)
        for index, group in enumerate(groups):
            if group["class"] != track["class"]:
                continue
            if current_views & group["views"]:
                continue
            distance = float(np.linalg.norm(current_centroid - group["centroid"]))
            if distance > centroid_distance:
                continue
            shared = np.intersect1d(current["keys"], group["keys"], assume_unique=True).size
            overlap = shared / max(1, min(len(current["keys"]), len(group["keys"])))
            if overlap >= overlap_threshold and overlap > best_overlap:
                best_overlap = overlap
                best_index = index

        if best_index is None:
            bbox_min = np.asarray(track["bbox_min"], dtype=np.float64)
            bbox_max = np.asarray(track["bbox_max"], dtype=np.float64)
            groups.append(
                {
                    "class": track["class"],
                    "source_tracks": [track],
                    "views": set(current_views),
                    "keys": current["keys"],
                    "centroid": current_centroid,
                    "bbox_min": bbox_min,
                    "bbox_max": bbox_max,
                    "merge_overlaps": [],
                }
            )
        else:
            group = groups[best_index]
            group["source_tracks"].append(track)
            group["views"].update(current_views)
            group["keys"] = np.union1d(group["keys"], current["keys"])
            weights = np.asarray(
                [max(1, int(item.get("exported_point_count", item.get("point_count", 1)))) for item in group["source_tracks"]],
                dtype=np.float64,
            )
            centroids = np.stack(
                [np.asarray(item["centroid"], dtype=np.float64) for item in group["source_tracks"]]
            )
            group["centroid"] = np.average(centroids, axis=0, weights=weights)
            group["bbox_min"] = np.minimum(
                group["bbox_min"], np.asarray(track["bbox_min"], dtype=np.float64)
            )
            group["bbox_max"] = np.maximum(
                group["bbox_max"], np.asarray(track["bbox_max"], dtype=np.float64)
            )
            group["merge_overlaps"].append(best_overlap)
    return groups


def camera_matrix(image, width: int, height: int) -> o3d.camera.PinholeCameraIntrinsic:
    camera = image.camera
    params = np.asarray(camera.params, dtype=np.float64)
    model = str(camera.model_name)
    if model == "PINHOLE":
        fx, fy, cx, cy = params[:4]
    elif model == "SIMPLE_PINHOLE":
        fx, cx, cy = params[:3]
        fy = fx
    else:
        raise RuntimeError(f"Stage 4 expects rectified PINHOLE input; got {model}")
    sx = width / float(camera.width)
    sy = height / float(camera.height)
    intrinsic = o3d.camera.PinholeCameraIntrinsic()
    intrinsic.set_intrinsics(
        width,
        height,
        float(fx * sx),
        float(fy * sy),
        float(cx * sx),
        float(cy * sy),
    )
    return intrinsic


def camera_extrinsic(image) -> np.ndarray:
    pose = image.cam_from_world()
    matrix = np.asarray(pose.matrix(), dtype=np.float64)
    extrinsic = np.eye(4, dtype=np.float64)
    extrinsic[:3, :4] = matrix
    return extrinsic


def decode_depth(path: Path, encode_scale: float, metric_scale: float) -> np.ndarray:
    encoded = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if encoded is None:
        raise RuntimeError(f"Cannot read VGGT depth: {path}")
    encoded = encoded.astype(np.float32)
    depth = np.zeros_like(encoded, dtype=np.float32)
    valid = encoded > 0
    depth[valid] = float(encode_scale) / encoded[valid]
    depth *= float(metric_scale)
    depth[~np.isfinite(depth)] = 0.0
    return depth


def lookup_fragment_masks(
    group: dict,
    fragment_by_id: dict[int, dict],
    stage1: Path,
) -> list[dict]:
    observations = []
    seen = set()
    for track in group["source_tracks"]:
        for fragment_id in track.get("fragment_ids", []):
            fragment = fragment_by_id.get(int(fragment_id))
            if fragment is None:
                continue
            key = (fragment["image"], int(fragment["local_id"]))
            if key in seen:
                continue
            seen.add(key)
            frame_json = stage1 / "instances" / f"{Path(fragment['image']).stem}.json"
            record = json.loads(frame_json.read_text())
            match = next(
                (item for item in record.get("instances", []) if int(item["local_id"]) == int(fragment["local_id"])),
                None,
            )
            if match is None or match.get("class") != group["class"]:
                continue
            mask_path = stage1 / match["mask"]
            observations.append(
                {
                    "image": fragment["image"],
                    "local_id": int(fragment["local_id"]),
                    "mask": mask_path,
                    "score": float(match.get("score", fragment.get("detector_score", 0.0))),
                }
            )
    observations.sort(key=lambda item: item["image"])
    return observations


def cleanup_mesh(
    mesh: o3d.geometry.TriangleMesh,
    min_triangles: int,
    min_fraction: float,
    max_triangles: int,
) -> tuple[o3d.geometry.TriangleMesh, dict]:
    mesh.remove_duplicated_vertices()
    mesh.remove_duplicated_triangles()
    mesh.remove_degenerate_triangles()
    mesh.remove_unreferenced_vertices()
    triangles_before = len(mesh.triangles)
    removed_components = 0
    if triangles_before:
        labels, counts, _ = mesh.cluster_connected_triangles()
        labels = np.asarray(labels)
        counts = np.asarray(counts)
        if len(counts):
            threshold = max(int(min_triangles), int(np.max(counts) * float(min_fraction)))
            remove = np.zeros(triangles_before, dtype=bool)
            for component_id, count in enumerate(counts):
                if int(count) < threshold:
                    remove[labels == component_id] = True
                    removed_components += 1
            if np.any(remove):
                mesh.remove_triangles_by_mask(remove)
                mesh.remove_unreferenced_vertices()
    if max_triangles > 0 and len(mesh.triangles) > max_triangles:
        mesh = mesh.simplify_quadric_decimation(target_number_of_triangles=int(max_triangles))
    mesh.compute_vertex_normals()
    return mesh, {
        "triangles_before_cleanup": int(triangles_before),
        "triangles_after_cleanup": int(len(mesh.triangles)),
        "vertices_after_cleanup": int(len(mesh.vertices)),
        "removed_small_components": int(removed_components),
    }


def integrate_object(
    observations: list[dict],
    images_by_name: dict,
    frames_dir: Path,
    depth_dir: Path,
    confidence_dir: Path,
    encode_scale: float,
    metric_scale: float,
    voxel_size: float,
    truncation: float,
    confidence_percentile: float,
    min_mask_pixels: int,
    depth_trunc_quantile: float,
    bbox_min: np.ndarray,
    bbox_max: np.ndarray,
    max_volume_resolution: int,
    volume_margin_scale: float,
    volume_margin_min: float,
) -> tuple[o3d.geometry.TriangleMesh, dict]:
    extent = np.maximum(bbox_max - bbox_min, 1e-6)
    max_extent = float(np.max(extent))
    margin = max(
        float(volume_margin_min),
        float(volume_margin_scale) * max_extent,
        2.0 * float(truncation),
    )
    center = 0.5 * (bbox_min + bbox_max)
    volume_length = max_extent + 2.0 * margin
    requested_resolution = int(np.ceil(volume_length / max(float(voxel_size), 1e-6)))
    resolution = max(32, min(int(max_volume_resolution), requested_resolution))
    effective_voxel = volume_length / resolution
    effective_truncation = max(float(truncation), 4.0 * effective_voxel)
    origin = center - 0.5 * volume_length
    volume = o3d.pipelines.integration.UniformTSDFVolume(
        length=float(volume_length),
        resolution=int(resolution),
        sdf_trunc=float(effective_truncation),
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
        origin=origin.astype(np.float64),
    )
    used = []
    valid_depth_values = []

    # Decode/filter observations first so one robust depth-truncation value is
    # used throughout the object's volume integration.
    prepared = []
    for observation in observations:
        image_name = observation["image"]
        depth = decode_depth(
            depth_dir / f"{Path(image_name).stem}.png",
            encode_scale,
            metric_scale,
        )
        mask = cv2.imread(str(observation["mask"]), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            continue
        mask = cv2.resize(mask, (depth.shape[1], depth.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
        confidence = np.load(confidence_dir / f"{Path(image_name).stem}.npy").astype(np.float32)
        if confidence.shape != depth.shape:
            confidence = cv2.resize(
                confidence,
                (depth.shape[1], depth.shape[0]),
                interpolation=cv2.INTER_LINEAR,
            )
        candidate = mask & np.isfinite(confidence) & (depth > 0)
        if np.count_nonzero(candidate) < min_mask_pixels:
            continue
        threshold = float(np.percentile(confidence[candidate], confidence_percentile))
        valid = candidate & (confidence >= threshold)
        if np.count_nonzero(valid) < min_mask_pixels:
            continue
        valid_depth_values.append(depth[valid])
        prepared.append((observation, depth, valid))

    if not prepared:
        return o3d.geometry.TriangleMesh(), {
            "views_used": 0,
            "observations": [],
            "volume_length": volume_length,
            "volume_resolution": resolution,
            "effective_voxel_size": effective_voxel,
            "effective_sdf_truncation": effective_truncation,
        }
    concatenated = np.concatenate(valid_depth_values)
    depth_trunc = float(np.quantile(concatenated, depth_trunc_quantile) * 1.05)

    for observation, depth, valid in prepared:
        image_name = observation["image"]
        source = cv2.imread(str(frames_dir / image_name), cv2.IMREAD_COLOR)
        if source is None:
            continue
        source = cv2.cvtColor(source, cv2.COLOR_BGR2RGB)
        source = cv2.resize(source, (depth.shape[1], depth.shape[0]), interpolation=cv2.INTER_AREA)
        masked_depth = np.where(valid, depth, 0.0).astype(np.float32)
        masked_color = source.copy()
        masked_color[~valid] = 0
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(masked_color.astype(np.uint8)),
            o3d.geometry.Image(masked_depth),
            depth_scale=1.0,
            depth_trunc=depth_trunc,
            convert_rgb_to_intensity=False,
        )
        camera = images_by_name.get(image_name)
        if camera is None:
            continue
        intrinsic = camera_matrix(camera, depth.shape[1], depth.shape[0])
        volume.integrate(rgbd, intrinsic, camera_extrinsic(camera))
        used.append(
            {
                "image": image_name,
                "local_id": observation["local_id"],
                "detector_score": observation["score"],
                "valid_pixels": int(np.count_nonzero(valid)),
            }
        )

    mesh = volume.extract_triangle_mesh()
    return mesh, {
        "views_used": len(used),
        "depth_trunc": depth_trunc,
        "volume_origin": origin.tolist(),
        "volume_length": volume_length,
        "volume_resolution": resolution,
        "effective_voxel_size": effective_voxel,
        "effective_sdf_truncation": effective_truncation,
        "observations": used,
    }


def export_glb(mesh: o3d.geometry.TriangleMesh, path: Path) -> None:
    vertices = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.triangles)
    colors = np.asarray(mesh.vertex_colors)
    kwargs = {}
    if len(colors) == len(vertices):
        kwargs["vertex_colors"] = np.clip(colors * 255.0, 0, 255).astype(np.uint8)
    tri = trimesh.Trimesh(vertices=vertices, faces=faces, process=False, **kwargs)
    tri.export(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Namma Stage 4: per-object observed-surface meshes")
    parser.add_argument("scene")
    parser.add_argument("--overwrite", action="store_true", default=env_bool("NS_MESH_OVERWRITE"))
    args = parser.parse_args()

    cfg = load_config()["meshing"]
    voxel_size = float(os.environ.get("NS_TSDF_VOXEL_SIZE", cfg["tsdf_voxel_size"]))
    truncation = float(os.environ.get("NS_TSDF_TRUNCATION", cfg["tsdf_truncation"]))
    confidence_percentile = float(
        os.environ.get("NS_MESH_CONFIDENCE_PERCENTILE", cfg["confidence_percentile"])
    )
    min_views = int(os.environ.get("NS_MESH_MIN_VIEWS", cfg["min_views"]))
    min_mask_pixels = int(os.environ.get("NS_MESH_MIN_MASK_PIXELS", cfg["min_mask_pixels"]))
    duplicate_overlap = float(
        os.environ.get("NS_MESH_DUPLICATE_OVERLAP", cfg["duplicate_overlap_threshold"])
    )
    max_mesh_triangles = int(
        os.environ.get("NS_MAX_MESH_TRIANGLES", cfg["max_mesh_triangles"])
    )

    base = SEMANTIC_ROOT / args.scene
    stage1 = base / "01_segmentation"
    stage2 = base / "02_semantic_cloud"
    stage2_summary = json.loads((stage2 / "summary.json").read_text())
    if not stage2_summary.get("complete"):
        raise RuntimeError("Stage 4 requires a complete Stage-2 semantic cloud.")
    tracks = json.loads((stage2 / "instance_tracks.json").read_text())
    fragments = json.loads((stage2 / "fragments.json").read_text())
    fragment_by_id = {int(item["fragment_id"]): item for item in fragments}

    nextgen = NEXTGEN_ROOT / args.scene
    vggt_summary = json.loads((nextgen / "02_vggt/summary.json").read_text())
    alignment = json.loads((nextgen / "04_gs_dataset/vggt_to_colmap_alignment.json").read_text())
    encode_scale = float(vggt_summary["inverse_depth_encode_scale"])
    metric_scale = float(alignment["scale"])
    reconstruction = pycolmap.Reconstruction(str(nextgen / "03_sfm/model"))
    images_by_name = {image.name: image for image in reconstruction.images.values()}

    output = base / "04_object_meshes"
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Object-mesh output already exists: {output}. Use --overwrite to replace it.")
        shutil.rmtree(output)
    objects_dir = output / "objects"
    objects_dir.mkdir(parents=True)

    groups = consolidate_tracks(
        tracks,
        stage2 / "instances",
        duplicate_overlap,
        float(cfg["duplicate_voxel_size"]),
        float(cfg["duplicate_centroid_distance"]),
    )
    counters = Counter()
    results = []
    scene = trimesh.Scene()

    print("=== NAMMA STAGE 4: OBJECT MESHING ===")
    print(f"Scene:                 {args.scene}")
    print(f"Exported Stage-2 tracks: {sum(1 for track in tracks if track.get('exported'))}")
    print(f"Consolidated objects:  {len(groups)}")
    print(f"TSDF voxel/trunc:      {voxel_size}/{truncation}")

    for group_index, group in enumerate(groups, start=1):
        observations = lookup_fragment_masks(group, fragment_by_id, stage1)
        if len({item["image"] for item in observations}) < min_views:
            continue
        mesh, integration = integrate_object(
            observations,
            images_by_name,
            nextgen / "01_frames/images",
            nextgen / "02_vggt/depths",
            nextgen / "02_vggt/confidence",
            encode_scale,
            metric_scale,
            voxel_size,
            truncation,
            confidence_percentile,
            min_mask_pixels,
            float(cfg["depth_trunc_quantile"]),
            np.asarray(group["bbox_min"], dtype=np.float64),
            np.asarray(group["bbox_max"], dtype=np.float64),
            int(cfg["max_volume_resolution"]),
            float(cfg["volume_margin_scale"]),
            float(cfg["volume_margin_min"]),
        )
        if integration["views_used"] < min_views or len(mesh.triangles) == 0:
            results.append(
                {
                    "class": group["class"],
                    "source_tracks": [item["persistent_id"] for item in group["source_tracks"]],
                    "status": "skipped_no_mesh",
                    **integration,
                }
            )
            continue
        mesh, cleanup = cleanup_mesh(
            mesh,
            int(cfg["component_min_triangles"]),
            float(cfg["component_min_fraction"]),
            max_mesh_triangles,
        )
        if len(mesh.triangles) == 0:
            continue

        counters[group["class"]] += 1
        object_id = f"{group['class']}_{counters[group['class']]:03d}"
        object_dir = objects_dir / object_id
        object_dir.mkdir(parents=True)
        o3d.io.write_triangle_mesh(str(object_dir / "observed_mesh.ply"), mesh, write_ascii=False)
        export_glb(mesh, object_dir / "observed_mesh.glb")
        tri = trimesh.load(object_dir / "observed_mesh.glb", force="mesh")
        scene.add_geometry(tri, geom_name=object_id, node_name=object_id)

        record = {
            "object_id": object_id,
            "class": group["class"],
            "source_tracks": [item["persistent_id"] for item in group["source_tracks"]],
            "source_track_count": len(group["source_tracks"]),
            "source_views": sorted(group["views"]),
            "duplicate_merge_overlaps": group["merge_overlaps"],
            "status": "meshed",
            **integration,
            **cleanup,
            "provenance": "observed masked VGGT depth fused with BA camera poses; no unseen completion",
        }
        (object_dir / "metadata.json").write_text(json.dumps(record, indent=2))
        results.append(record)
        print(
            f"[mesh] {group_index}/{len(groups)} {object_id}: "
            f"{integration['views_used']} views, {len(mesh.vertices)} verts, {len(mesh.triangles)} tris"
        )

    if scene.geometry:
        scene.export(output / "objects_preview.glb")

    meshed = [item for item in results if item.get("status") == "meshed"]
    by_class = Counter(item["class"] for item in meshed)
    duplicate_merges = sum(max(0, len(group["source_tracks"]) - 1) for group in groups)
    summary = {
        "scene": args.scene,
        "complete": True,
        "stage2_exported_tracks": int(sum(1 for track in tracks if track.get("exported"))),
        "consolidated_candidates": len(groups),
        "duplicate_tracks_merged": int(duplicate_merges),
        "meshed_objects": len(meshed),
        "meshed_objects_by_class": dict(by_class),
        "skipped_objects": len(results) - len(meshed),
        "tsdf": {
            "voxel_size": voxel_size,
            "sdf_truncation": truncation,
            "confidence_percentile": confidence_percentile,
            "min_views": min_views,
            "min_mask_pixels": min_mask_pixels,
        },
        "depth_source": "decoded VGGT-Omega inverse-depth PNGs",
        "depth_to_ba_scale": metric_scale,
        "camera_source": "COLMAP global-BA model",
        "completion": "none",
        "objects_preview": str(output / "objects_preview.glb"),
    }
    (output / "objects.json").write_text(json.dumps(results, indent=2))
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    (output / ".complete").write_text("ok\n")
    print("=== STAGE 4 COMPLETE ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
