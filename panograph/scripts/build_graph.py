#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from common import env_bool, env_float, env_int, load_config, scene_root


def fallback_positions(count: int) -> tuple[np.ndarray, str]:
    radius = max(2.0, 0.45 * count)
    angles = np.linspace(0, 2 * np.pi, count, endpoint=False)
    points = np.stack([radius * np.cos(angles), radius * np.sin(angles)], axis=1)
    return points, "fallback_circle"


def flatten_centers(centers: np.ndarray, target_median_step: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    centered = centers - centers.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    if vt.shape[0] < 2:
        raise RuntimeError("Not enough pose variation to construct a floor projection.")
    axis_x = vt[0]
    axis_z = vt[1]
    points = np.stack([centered @ axis_x, centered @ axis_z], axis=1)
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    valid = steps[np.isfinite(steps) & (steps > 1e-6)]
    if len(valid):
        scale = target_median_step / float(np.median(valid))
        points *= scale
    return points, axis_x, axis_z


def gps_to_local_enu(stations: list[dict]) -> np.ndarray | None:
    if not all("latitude" in station and "longitude" in station for station in stations):
        return None
    lat = np.radians(np.array([float(station["latitude"]) for station in stations]))
    lon = np.radians(np.array([float(station["longitude"]) for station in stations]))
    lat0 = float(lat.mean())
    lon0 = float(lon.mean())
    radius_m = 6_378_137.0
    east = radius_m * (lon - lon0) * math.cos(lat0)
    north = radius_m * (lat - lat0)
    return np.stack([east, north], axis=1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scene", nargs="?", default="hindoor_unibs")
    args = parser.parse_args()
    cfg = load_config()["graph"]
    root = scene_root(args.scene)
    manifest_file = root / "01_panoramas" / "manifest.json"
    if not manifest_file.is_file():
        raise RuntimeError(f"Missing panorama manifest: {manifest_file}")
    manifest = json.loads(manifest_file.read_text())
    stations = manifest["stations"]
    n = len(stations)
    target_step = float(cfg.get("median_edge_length", 2.0))
    camera_height = env_float("NP_CAMERA_HEIGHT", float(cfg["camera_height"]))
    yaw_sign = env_float("NP_YAW_SIGN", float(cfg.get("yaw_sign", 1.0)))
    yaw_offset = math.radians(env_float("NP_YAW_OFFSET_DEG", float(cfg.get("yaw_offset_deg", 0.0))))

    poses_file = root / "02_geometry" / "station_c2w.npy"
    poses = None
    if poses_file.is_file():
        candidate = np.load(poses_file)
        if candidate.shape == (n, 4, 4) and np.isfinite(candidate).all():
            poses = candidate

    gps_points = gps_to_local_enu(stations)
    if gps_points is not None:
        points2d = gps_points
        axis_x = axis_z = None
        layout_method = "exif_gps_local_enu"
    elif poses is not None:
        centers = poses[:, :3, 3]
        try:
            points2d, axis_x, axis_z = flatten_centers(centers, target_step)
            layout_method = "pca_floor_projection_of_pairwise_panovggt"
        except Exception as exc:
            print(f"[warn] pose flattening failed ({exc}); using deterministic fallback layout")
            points2d, layout_method = fallback_positions(n)
            axis_x = axis_z = None
    else:
        print("[warn] PanoVGGT poses unavailable; viewer graph will use a deterministic fallback circle.")
        points2d, layout_method = fallback_positions(n)
        axis_x = axis_z = None

    nodes = []
    for i, station in enumerate(stations):
        yaw = 0.0
        if gps_points is not None and "heading_degrees" in station:
            yaw = yaw_sign * math.radians(float(station["heading_degrees"])) + yaw_offset
        elif poses is not None and axis_x is not None and axis_z is not None:
            forward = poses[i][:3, 2]
            fx = float(np.dot(forward, axis_x))
            fz = float(np.dot(forward, axis_z))
            if abs(fx) + abs(fz) > 1e-8:
                yaw = yaw_sign * math.atan2(fx, fz) + yaw_offset
        x, z = map(float, points2d[i])
        nodes.append({
            "id": station["id"],
            "index": i,
            "original_name": station["original_name"],
            "position": [x, camera_height, z],
            "floor_position": [x, 0.0, z],
            "yaw": yaw,
            "web_panorama": station["web"],
        })

    edge_set: set[tuple[int, int]] = set()
    for i in range(n - 1):
        edge_set.add((i, i + 1))

    add_knn = bool(cfg.get("add_knn_edges", True))
    k = env_int("NP_GRAPH_KNN", int(cfg.get("knn_k", 2)))
    radius_factor = env_float("NP_GRAPH_RADIUS_FACTOR", float(cfg.get("knn_radius_factor", 1.8)))
    if add_knn and n >= 3:
        dmat = np.linalg.norm(points2d[:, None, :] - points2d[None, :, :], axis=2)
        consecutive = [dmat[i, i + 1] for i in range(n - 1) if dmat[i, i + 1] > 1e-6]
        radius = (float(np.median(consecutive)) if consecutive else target_step) * radius_factor
        for i in range(n):
            candidates = np.argsort(dmat[i])
            added = 0
            for j in candidates:
                if i == j or dmat[i, j] > radius:
                    continue
                a, b = sorted((int(i), int(j)))
                edge_set.add((a, b))
                added += 1
                if added >= k:
                    break

    edges = []
    neighbors = {node["id"]: [] for node in nodes}
    for a, b in sorted(edge_set):
        distance = float(np.linalg.norm(points2d[a] - points2d[b]))
        edges.append({"a": nodes[a]["id"], "b": nodes[b]["id"], "distance": distance})
        neighbors[nodes[a]["id"]].append(nodes[b]["id"])
        neighbors[nodes[b]["id"]].append(nodes[a]["id"])
    for node in nodes:
        node["neighbors"] = neighbors[node["id"]]

    graph = {
        "scene": args.scene,
        "layout_method": layout_method,
        "units": "meters" if gps_points is not None else "viewer_relative_units",
        "nodes": nodes,
        "edges": edges,
    }
    output = root / "03_graph"
    output.mkdir(parents=True, exist_ok=True)
    (output / "graph.json").write_text(json.dumps(graph, indent=2))
    (output / "summary.json").write_text(json.dumps({
        "scene": args.scene,
        "complete": True,
        "layout_method": layout_method,
        "nodes": len(nodes),
        "edges": len(edges),
    }, indent=2))
    print("=== NAMMA PANOGRAPH: GRAPH COMPLETE ===")
    print(json.dumps({"layout_method": layout_method, "nodes": len(nodes), "edges": len(edges)}, indent=2))


if __name__ == "__main__":
    main()
