#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
import pycolmap
import trimesh
import yaml
from scipy.spatial import ConvexHull, cKDTree


SEMANTIC_ROOT = Path("/workspace/semantic")
NEXTGEN_ROOT = Path("/workspace/nextgen")
DEFAULT_CONFIG = Path("/opt/namma-semantic/config/default.yaml")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> dict:
    with DEFAULT_CONFIG.open() as handle:
        return yaml.safe_load(handle) or {}


def load_points(path: Path) -> np.ndarray:
    if not path.is_file():
        return np.empty((0, 3), dtype=np.float64)
    cloud = o3d.io.read_point_cloud(str(path))
    return np.asarray(cloud.points, dtype=np.float64)


def normalize_plane(plane: np.ndarray) -> np.ndarray:
    plane = np.asarray(plane, dtype=np.float64)
    norm = np.linalg.norm(plane[:3])
    if norm < 1e-12:
        raise RuntimeError("Degenerate plane normal")
    return plane / norm


def fit_plane(points: np.ndarray, threshold: float, iterations: int) -> tuple[np.ndarray, np.ndarray]:
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    plane, inliers = cloud.segment_plane(
        distance_threshold=float(threshold),
        ransac_n=3,
        num_iterations=int(iterations),
    )
    return normalize_plane(np.asarray(plane)), np.asarray(inliers, dtype=np.int64)


def camera_centers(model_dir: Path) -> np.ndarray:
    reconstruction = pycolmap.Reconstruction(str(model_dir))
    centers = []
    for image in reconstruction.images.values():
        value = image.projection_center
        value = value() if callable(value) else value
        centers.append(np.asarray(value, dtype=np.float64).reshape(3))
    return np.asarray(centers)


def plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    normal = normal / np.linalg.norm(normal)
    axis = np.array([1.0, 0.0, 0.0])
    if abs(np.dot(axis, normal)) > 0.9:
        axis = np.array([0.0, 1.0, 0.0])
    u = axis - np.dot(axis, normal) * normal
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    v /= np.linalg.norm(v)
    return u, v


def polygon_mesh_from_plane(
    points: np.ndarray,
    plane: np.ndarray,
    clip_quantile: float,
    simplify_tolerance: float = 0.0,
    max_boundary_vertices: int = 0,
) -> tuple[trimesh.Trimesh, dict]:
    normal = plane[:3]
    centroid = points.mean(axis=0)
    centroid = centroid - (np.dot(normal, centroid) + plane[3]) * normal
    u, v = plane_basis(normal)
    projected = np.column_stack(((points - centroid) @ u, (points - centroid) @ v))

    # A tiny number of coplanar semantic outliers can explode a convex hull.
    # Clip only extreme radial support before building the room-shell polygon.
    radial = np.linalg.norm(projected - np.median(projected, axis=0), axis=1)
    if len(radial) >= 20:
        cutoff = np.quantile(radial, clip_quantile)
        projected = projected[radial <= cutoff]
    if len(projected) < 3:
        raise RuntimeError("Not enough planar support for polygonization")

    hull = ConvexHull(projected)
    polygon_2d = projected[hull.vertices]
    if simplify_tolerance > 0 and len(polygon_2d) > 3:
        contour = polygon_2d.astype(np.float32).reshape(-1, 1, 2)
        simplified = cv2.approxPolyDP(
            contour,
            epsilon=float(simplify_tolerance),
            closed=True,
        ).reshape(-1, 2)
        if len(simplified) >= 3:
            polygon_2d = simplified.astype(np.float64)
    if max_boundary_vertices > 0 and len(polygon_2d) > max_boundary_vertices:
        # Increase epsilon gradually until the observed hull is represented by
        # a compact polygon rather than a saw-tooth boundary.
        perimeter = float(cv2.arcLength(polygon_2d.astype(np.float32).reshape(-1, 1, 2), True))
        for fraction in np.linspace(0.002, 0.08, 40):
            simplified = cv2.approxPolyDP(
                polygon_2d.astype(np.float32).reshape(-1, 1, 2),
                epsilon=perimeter * float(fraction),
                closed=True,
            ).reshape(-1, 2)
            if 3 <= len(simplified) <= max_boundary_vertices:
                polygon_2d = simplified.astype(np.float64)
                break
    vertices = centroid + polygon_2d[:, :1] * u + polygon_2d[:, 1:] * v
    faces = []
    for index in range(1, len(vertices) - 1):
        faces.append([0, index, index + 1])
    mesh = trimesh.Trimesh(
        vertices=np.asarray(vertices, dtype=np.float64),
        faces=np.asarray(faces, dtype=np.int64),
        process=False,
    )
    return mesh, {
        "centroid": centroid.tolist(),
        "hull_vertices": vertices.tolist(),
        "area": float(mesh.area),
    }


def rectangular_wall_mesh(
    points: np.ndarray,
    plane: np.ndarray,
    up: np.ndarray,
    extent_quantile: float,
) -> tuple[trimesh.Trimesh, dict]:
    """Regularize an observed vertical wall plane into one clean rectangle.

    The rectangle is bounded by robust quantiles of the actual inlier support;
    this fills small observation holes but does not extrapolate beyond the
    observed wall extent.
    """
    normal = plane[:3] / np.linalg.norm(plane[:3])
    vertical = up - np.dot(up, normal) * normal
    vertical /= max(np.linalg.norm(vertical), 1e-12)
    horizontal = np.cross(vertical, normal)
    horizontal /= max(np.linalg.norm(horizontal), 1e-12)
    centroid = points.mean(axis=0)
    centroid = centroid - (np.dot(normal, centroid) + plane[3]) * normal
    rel = points - centroid
    x = rel @ horizontal
    y = rel @ vertical
    q = float(np.clip(extent_quantile, 0.0, 0.2))
    x0, x1 = np.quantile(x, [q, 1.0 - q])
    y0, y1 = np.quantile(y, [q, 1.0 - q])
    corners_2d = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64)
    vertices = (
        centroid
        + corners_2d[:, :1] * horizontal
        + corners_2d[:, 1:] * vertical
    )
    faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    return mesh, {
        "centroid": centroid.tolist(),
        "hull_vertices": vertices.tolist(),
        "area": float(mesh.area),
        "regularized_rectangle": True,
    }


def export_mesh(mesh: trimesh.Trimesh, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(path)


def iterative_wall_planes(
    points: np.ndarray,
    up: np.ndarray,
    threshold: float,
    min_points: int,
    iterations: int,
    max_planes: int,
    vertical_dot_max: float,
    clip_quantile: float,
    regularize_rectangles: bool,
    wall_extent_quantile: float,
) -> list[dict]:
    remaining = points.copy()
    outputs: list[dict] = []
    attempts = 0
    while len(remaining) >= min_points and len(outputs) < max_planes and attempts < max_planes * 3:
        attempts += 1
        plane, inliers = fit_plane(remaining, threshold, iterations)
        if len(inliers) < min_points:
            break
        support = remaining[inliers]
        keep = np.ones(len(remaining), dtype=bool)
        keep[inliers] = False
        remaining = remaining[keep]

        alignment = abs(float(np.dot(plane[:3], up)))
        if alignment > vertical_dot_max:
            continue
        if regularize_rectangles:
            mesh, geometry = rectangular_wall_mesh(
                support,
                plane,
                up,
                wall_extent_quantile,
            )
        else:
            mesh, geometry = polygon_mesh_from_plane(support, plane, clip_quantile)
        outputs.append(
            {
                "plane": plane,
                "support": support,
                "mesh": mesh,
                "inlier_count": int(len(inliers)),
                "vertical_alignment": alignment,
                **geometry,
            }
        )
    return outputs


def pillar_meshes(
    points: np.ndarray,
    plane_threshold: float,
    min_points: int,
    eps_scale: float,
    dbscan_min_samples: int,
) -> list[dict]:
    if len(points) < min_points:
        return []
    tree = cKDTree(points)
    distances, _ = tree.query(points, k=2)
    spacing = float(np.median(distances[:, 1]))
    eps = max(float(plane_threshold) * 2.0, spacing * float(eps_scale))
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    labels = np.asarray(
        cloud.cluster_dbscan(
            eps=eps,
            min_points=max(3, int(dbscan_min_samples)),
            print_progress=False,
        )
    )
    outputs = []
    for label in sorted(value for value in np.unique(labels) if value >= 0):
        cluster = points[labels == label]
        if len(cluster) < min_points:
            continue
        cluster_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(cluster))
        obb = cluster_cloud.get_oriented_bounding_box(robust=True)
        extent = np.asarray(obb.extent, dtype=np.float64)
        if np.any(extent <= 1e-6):
            continue
        mesh = trimesh.creation.box(
            extents=extent,
            transform=np.block(
                [
                    [np.asarray(obb.R, dtype=np.float64), np.asarray(obb.center, dtype=np.float64).reshape(3, 1)],
                    [np.zeros((1, 3)), np.ones((1, 1))],
                ]
            ),
        )
        outputs.append(
            {
                "mesh": mesh,
                "point_count": int(len(cluster)),
                "center": np.asarray(obb.center).tolist(),
                "extent": extent.tolist(),
                "rotation": np.asarray(obb.R).tolist(),
                "cluster_eps": eps,
            }
        )
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Namma Stage 3: clean room-layout geometry")
    parser.add_argument("scene")
    parser.add_argument("--overwrite", action="store_true", default=env_bool("NS_LAYOUT_OVERWRITE"))
    args = parser.parse_args()

    config = load_config()["layout"]
    threshold = float(
        os.environ.get("NS_LAYOUT_DISTANCE_THRESHOLD", config["plane_distance_threshold"])
    )
    min_points = int(os.environ.get("NS_LAYOUT_MIN_POINTS", config["plane_min_points"]))
    max_walls = int(os.environ.get("NS_LAYOUT_MAX_WALLS", config["max_wall_planes"]))
    iterations = int(config["ransac_iterations"])
    vertical_dot_max = float(config["wall_vertical_dot_max"])
    ceiling_parallel_dot_min = float(config["ceiling_parallel_dot_min"])
    clip_quantile = float(config["hull_clip_quantile"])
    simplify_tolerance = float(config["simplify_tolerance"])
    wall_extent_quantile = float(config["wall_extent_quantile"])
    wall_regularize_rectangles = bool(config["wall_regularize_rectangles"])
    floor_max_boundary_vertices = int(config["floor_max_boundary_vertices"])
    pillar_min_points = int(config["pillar_min_points"])
    pillar_eps_scale = float(config["pillar_cluster_eps_scale"])
    pillar_dbscan_min_samples = int(config["pillar_dbscan_min_samples"])

    base = SEMANTIC_ROOT / args.scene
    stage2 = base / "02_semantic_cloud"
    summary_file = stage2 / "summary.json"
    if not summary_file.is_file() or not json.loads(summary_file.read_text()).get("complete"):
        raise RuntimeError("Stage 3 requires a complete Stage-2 semantic cloud.")

    classes = stage2 / "classes"
    output = base / "03_layout"
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Layout output already exists: {output}. Use --overwrite to replace it.")
        shutil.rmtree(output)
    meshes_dir = output / "meshes"
    support_dir = output / "support"
    meshes_dir.mkdir(parents=True)
    support_dir.mkdir(parents=True)

    floor_points = load_points(classes / "floor.ply")
    wall_points = load_points(classes / "wall.ply")
    ceiling_points = load_points(classes / "ceiling.ply")
    pillar_points = load_points(classes / "pillar.ply")
    if len(floor_points) < min_points:
        raise RuntimeError(
            f"Need at least {min_points} floor points for a stable layout; found {len(floor_points)}."
        )

    print("=== NAMMA STAGE 3: LAYOUT FITTING ===")
    print(f"Scene:          {args.scene}")
    print(f"Floor points:   {len(floor_points)}")
    print(f"Wall points:    {len(wall_points)}")
    print(f"Ceiling points: {len(ceiling_points)}")
    print(f"Pillar points:  {len(pillar_points)}")

    floor_plane, floor_inliers = fit_plane(floor_points, threshold, iterations)
    floor_support = floor_points[floor_inliers]
    centers = camera_centers(NEXTGEN_ROOT / args.scene / "03_sfm/model")
    camera_median = np.median(centers, axis=0)
    floor_centroid = floor_support.mean(axis=0)
    if np.dot(floor_plane[:3], camera_median - floor_centroid) < 0:
        floor_plane *= -1.0
    up = floor_plane[:3]
    floor_mesh, floor_geometry = polygon_mesh_from_plane(
        floor_support,
        floor_plane,
        clip_quantile,
        simplify_tolerance=simplify_tolerance,
        max_boundary_vertices=floor_max_boundary_vertices,
    )
    export_mesh(floor_mesh, meshes_dir / "floor.ply")
    o3d.io.write_point_cloud(
        str(support_dir / "floor_inliers.ply"),
        o3d.geometry.PointCloud(o3d.utility.Vector3dVector(floor_support)),
    )

    walls = iterative_wall_planes(
        wall_points,
        up,
        threshold,
        min_points,
        iterations,
        max_walls,
        vertical_dot_max,
        clip_quantile,
        wall_regularize_rectangles,
        wall_extent_quantile,
    ) if len(wall_points) >= min_points else []
    for index, wall in enumerate(walls, start=1):
        export_mesh(wall["mesh"], meshes_dir / f"wall_{index:03d}.ply")
        o3d.io.write_point_cloud(
            str(support_dir / f"wall_{index:03d}_inliers.ply"),
            o3d.geometry.PointCloud(o3d.utility.Vector3dVector(wall["support"])),
        )

    ceiling_record = None
    ceiling_mesh = None
    if len(ceiling_points) >= min_points:
        candidate, inliers = fit_plane(ceiling_points, threshold, iterations)
        alignment = abs(float(np.dot(candidate[:3], up)))
        if len(inliers) >= min_points and alignment >= ceiling_parallel_dot_min:
            support = ceiling_points[inliers]
            if np.dot(candidate[:3], up) > 0:
                candidate *= -1.0
            ceiling_mesh, geometry = polygon_mesh_from_plane(
                support,
                candidate,
                clip_quantile,
                simplify_tolerance=simplify_tolerance,
                max_boundary_vertices=floor_max_boundary_vertices,
            )
            export_mesh(ceiling_mesh, meshes_dir / "ceiling.ply")
            ceiling_record = {
                "plane": candidate.tolist(),
                "inlier_count": int(len(inliers)),
                "parallel_alignment": alignment,
                **geometry,
            }

    pillars = pillar_meshes(
        pillar_points,
        threshold,
        pillar_min_points,
        pillar_eps_scale,
        pillar_dbscan_min_samples,
    )
    for index, pillar in enumerate(pillars, start=1):
        export_mesh(pillar["mesh"], meshes_dir / f"pillar_{index:03d}.ply")

    scene = trimesh.Scene()
    scene.add_geometry(floor_mesh, geom_name="floor", node_name="floor")
    for index, wall in enumerate(walls, start=1):
        scene.add_geometry(
            wall["mesh"], geom_name=f"wall_{index:03d}", node_name=f"wall_{index:03d}"
        )
    if ceiling_mesh is not None:
        scene.add_geometry(ceiling_mesh, geom_name="ceiling", node_name="ceiling")
    for index, pillar in enumerate(pillars, start=1):
        scene.add_geometry(
            pillar["mesh"], geom_name=f"pillar_{index:03d}", node_name=f"pillar_{index:03d}"
        )
    scene.export(output / "room_shell.glb")

    layout = {
        "coordinate_frame": "COLMAP global BA",
        "up_vector": up.tolist(),
        "floor": {
            "plane": floor_plane.tolist(),
            "input_points": int(len(floor_points)),
            "inlier_count": int(len(floor_inliers)),
            **floor_geometry,
        },
        "walls": [
            {
                "id": f"wall_{index:03d}",
                "plane": wall["plane"].tolist(),
                "inlier_count": wall["inlier_count"],
                "vertical_alignment": wall["vertical_alignment"],
                "centroid": wall["centroid"],
                "hull_vertices": wall["hull_vertices"],
                "area": wall["area"],
            }
            for index, wall in enumerate(walls, start=1)
        ],
        "ceiling": ceiling_record,
        "pillars": [
            {
                "id": f"pillar_{index:03d}",
                **{key: value for key, value in pillar.items() if key != "mesh"},
            }
            for index, pillar in enumerate(pillars, start=1)
        ],
        "note": "No ceiling or unseen structural surfaces are synthesized when unsupported by observations.",
    }
    (output / "layout.json").write_text(json.dumps(layout, indent=2))
    summary = {
        "scene": args.scene,
        "complete": True,
        "floor_plane_found": True,
        "floor_inliers": int(len(floor_inliers)),
        "walls_found": len(walls),
        "ceiling_found": ceiling_record is not None,
        "pillars_found": len(pillars),
        "inputs": {
            "floor_points": int(len(floor_points)),
            "wall_points": int(len(wall_points)),
            "ceiling_points": int(len(ceiling_points)),
            "pillar_points": int(len(pillar_points)),
        },
        "thresholds": {
            "plane_distance_threshold": threshold,
            "plane_min_points": min_points,
            "max_wall_planes": max_walls,
            "wall_vertical_dot_max": vertical_dot_max,
            "ceiling_parallel_dot_min": ceiling_parallel_dot_min,
            "simplify_tolerance": simplify_tolerance,
            "wall_regularize_rectangles": wall_regularize_rectangles,
            "wall_extent_quantile": wall_extent_quantile,
            "floor_max_boundary_vertices": floor_max_boundary_vertices,
        },
        "room_shell": str(output / "room_shell.glb"),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    (output / ".complete").write_text("ok\n")

    print("=== STAGE 3 COMPLETE ===")
    print(json.dumps(summary, indent=2))
    print(f"Room shell: {output / 'room_shell.glb'}")


if __name__ == "__main__":
    main()
