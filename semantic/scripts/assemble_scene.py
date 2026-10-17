#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import numpy as np
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


def env_optional_float(name: str, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    parsed = float(value)
    if parsed <= 0:
        raise RuntimeError(f"{name} must be > 0; got {parsed}")
    return parsed


def load_single_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="mesh", process=False)
    if not isinstance(loaded, trimesh.Trimesh):
        raise RuntimeError(f"Expected one mesh in {path}, got {type(loaded).__name__}")
    if loaded.is_empty:
        raise RuntimeError(f"Mesh is empty: {path}")
    return loaded


def apply_scale(mesh: trimesh.Trimesh, scale: float | None) -> trimesh.Trimesh:
    output = mesh.copy()
    if scale is not None and abs(float(scale) - 1.0) > 1e-12:
        output.apply_scale(float(scale))
    return output


def mesh_bounds(mesh: trimesh.Trimesh) -> dict:
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    center = bounds.mean(axis=0)
    extents = bounds[1] - bounds[0]
    return {
        "min": bounds[0].tolist(),
        "max": bounds[1].tolist(),
        "center": center.tolist(),
        "extents": extents.tolist(),
    }


def scene_bounds(meshes: list[trimesh.Trimesh]) -> dict:
    if not meshes:
        return {"min": [0.0] * 3, "max": [0.0] * 3, "center": [0.0] * 3, "extents": [0.0] * 3}
    mins = np.stack([np.asarray(mesh.bounds[0], dtype=np.float64) for mesh in meshes])
    maxs = np.stack([np.asarray(mesh.bounds[1], dtype=np.float64) for mesh in meshes])
    bounds = np.stack([mins.min(axis=0), maxs.max(axis=0)])
    center = bounds.mean(axis=0)
    return {
        "min": bounds[0].tolist(),
        "max": bounds[1].tolist(),
        "center": center.tolist(),
        "extents": (bounds[1] - bounds[0]).tolist(),
    }


def safe_object_collision(mesh: trimesh.Trimesh, max_triangles: int) -> tuple[trimesh.Trimesh, str]:
    """Create a stable static collision proxy for noisy observed furniture.

    A convex hull is preferred for first-person collision because the Stage-4
    TSDF is intentionally an open observed surface. If the hull cannot be made
    robustly, fall back to an oriented box rather than returning an invalid
    collision mesh.
    """
    try:
        collision = mesh.convex_hull
        method = "convex_hull"
        if collision.is_empty or len(collision.faces) < 4:
            raise RuntimeError("degenerate convex hull")
    except Exception:
        collision = mesh.bounding_box_oriented.to_mesh()
        method = "oriented_box_fallback"

    if max_triangles > 0 and len(collision.faces) > max_triangles:
        # A conservative box is preferable to depending on optional external
        # mesh-simplification backends in the runtime image.
        collision = mesh.bounding_box_oriented.to_mesh()
        method = "oriented_box_triangle_cap"
    collision.remove_unreferenced_vertices()
    return collision, method


def architecture_collision(source_mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, str]:
    # Stage 3 already produced clean fitted floor/wall polygons and box pillars.
    return source_mesh.copy(), "fitted_source_mesh"


def export_collision(mesh: trimesh.Trimesh, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = mesh.copy()
    if getattr(clean.visual, "kind", None) == "texture":
        clean.visual = trimesh.visual.ColorVisuals(
            mesh=clean,
            face_colors=np.tile([140, 140, 140, 255], (len(clean.faces), 1)),
        )
    clean.export(path)


def camera_trajectory(model_dir: Path, scale: float | None) -> list[dict]:
    reconstruction = pycolmap.Reconstruction(str(model_dir))
    records = []
    for image in sorted(reconstruction.images.values(), key=lambda value: value.name):
        pose = image.cam_from_world()
        matrix = np.asarray(pose.matrix(), dtype=np.float64)
        world_from_camera = np.eye(4, dtype=np.float64)
        world_from_camera[:3, :3] = matrix[:, :3].T
        world_from_camera[:3, 3] = -matrix[:, :3].T @ matrix[:, 3]
        if scale is not None:
            world_from_camera[:3, 3] *= float(scale)
        forward = world_from_camera[:3, :3] @ np.array([0.0, 0.0, 1.0])
        camera_up = world_from_camera[:3, :3] @ np.array([0.0, -1.0, 0.0])
        records.append(
            {
                "image": image.name,
                "position": world_from_camera[:3, 3].tolist(),
                "forward": forward.tolist(),
                "camera_up": camera_up.tolist(),
                "world_from_camera": world_from_camera.tolist(),
            }
        )
    return records


def load_texture_records(stage5: Path) -> list[dict]:
    path = stage5 / "assets.json"
    if not path.is_file():
        raise RuntimeError(f"Missing Stage-5 asset metadata: {path}")
    return json.loads(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description="Namma Stage 6: final digital-twin scene assembly")
    parser.add_argument("scene")
    parser.add_argument("--overwrite", action="store_true", default=env_bool("NS_SCENE_OVERWRITE"))
    args = parser.parse_args()

    config = load_config()
    export_cfg = config["export"]
    configured_scale = export_cfg.get("meters_per_unit")
    meters_per_unit = env_optional_float("NS_SCENE_METERS_PER_UNIT", configured_scale)
    collision_max_triangles = int(
        os.environ.get(
            "NS_SCENE_COLLISION_MAX_TRIANGLES",
            export_cfg.get("collision_max_triangles", config["meshing"]["collision_max_triangles"]),
        )
    )

    base = SEMANTIC_ROOT / args.scene
    stage3 = base / "03_layout"
    stage4 = base / "04_object_meshes"
    stage5 = base / "05_textures"
    for required in [stage3 / "summary.json", stage4 / "summary.json", stage5 / "summary.json"]:
        if not required.is_file():
            raise RuntimeError(f"Stage 6 prerequisite missing: {required}")
        summary = json.loads(required.read_text())
        if not summary.get("complete"):
            raise RuntimeError(f"Stage prerequisite is not complete: {required}")

    output = base / "06_scene"
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Scene output already exists: {output}. Use --overwrite to replace it.")
        shutil.rmtree(output)
    collision_dir = output / "collisions"
    collision_dir.mkdir(parents=True)

    layout = json.loads((stage3 / "layout.json").read_text())
    texture_summary = json.loads((stage5 / "summary.json").read_text())
    texture_records = load_texture_records(stage5)

    visual_scene = trimesh.Scene()
    collision_scene = trimesh.Scene()
    scene_nodes = []
    visual_meshes: list[trimesh.Trimesh] = []
    collision_meshes: list[trimesh.Trimesh] = []
    visual_triangles = 0
    collision_triangles = 0

    print("=== NAMMA STAGE 6: DIGITAL-TWIN SCENE ASSEMBLY ===")
    print(f"Scene:                 {args.scene}")
    print(f"Stage-5 assets:        {len(texture_records)}")
    if meters_per_unit is None:
        print("Metric calibration:    UNCALIBRATED (reconstruction units)")
    else:
        print(f"Metric calibration:    {meters_per_unit} metres / reconstruction unit")

    for record in texture_records:
        asset_id = str(record["asset_id"])
        kind = str(record["kind"])
        class_name = str(record.get("class") or "unknown")
        if kind == "architecture":
            textured_path = stage5 / "architecture" / asset_id / "textured_mesh.glb"
            collision_source_path = stage3 / "meshes" / f"{asset_id}.ply"
        elif kind == "object":
            textured_path = stage5 / "objects" / asset_id / "textured_mesh.glb"
            collision_source_path = stage4 / "objects" / asset_id / "observed_mesh.ply"
        else:
            raise RuntimeError(f"Unknown Stage-5 asset kind {kind!r} for {asset_id}")
        if not textured_path.is_file():
            raise RuntimeError(f"Missing textured asset: {textured_path}")
        if not collision_source_path.is_file():
            raise RuntimeError(f"Missing collision source: {collision_source_path}")

        visual = apply_scale(load_single_mesh(textured_path), meters_per_unit)
        collision_source = apply_scale(load_single_mesh(collision_source_path), meters_per_unit)
        if kind == "architecture":
            collision, collision_method = architecture_collision(collision_source)
        else:
            collision, collision_method = safe_object_collision(
                collision_source,
                collision_max_triangles,
            )

        visual_scene.add_geometry(visual, geom_name=asset_id, node_name=asset_id)
        collision_scene.add_geometry(
            collision,
            geom_name=f"{asset_id}_collision",
            node_name=f"{asset_id}_collision",
        )
        visual_meshes.append(visual)
        collision_meshes.append(collision)
        visual_triangles += len(visual.faces)
        collision_triangles += len(collision.faces)

        collision_filename = f"{asset_id}.glb"
        export_collision(collision, collision_dir / collision_filename)
        node = {
            "id": asset_id,
            "parent": "root",
            "kind": kind,
            "class": class_name,
            "transform": np.eye(4).tolist(),
            "visual": {
                "source": str(textured_path.relative_to(base)),
                "scene_node": asset_id,
                "triangles": int(len(visual.faces)),
                "rgb_assigned_fraction": float(record.get("assigned_fraction", 0.0)),
            },
            "collision": {
                "mesh": str((collision_dir / collision_filename).relative_to(output)),
                "method": collision_method,
                "triangles": int(len(collision.faces)),
                "static": True,
            },
            "bounds": mesh_bounds(visual),
            "provenance": record.get("provenance"),
            "completion": "none",
        }
        if kind == "object":
            stage4_metadata = stage4 / "objects" / asset_id / "metadata.json"
            if stage4_metadata.is_file():
                object_metadata = json.loads(stage4_metadata.read_text())
                node["source_tracks"] = object_metadata.get("source_tracks", [])
                node["source_views"] = object_metadata.get("source_views", [])
        scene_nodes.append(node)
        print(
            f"[scene] {asset_id}: visual {len(visual.faces)} tris, "
            f"collision {len(collision.faces)} tris ({collision_method})"
        )

    if not visual_scene.geometry:
        raise RuntimeError("No Stage-5 visual assets were assembled.")

    visual_scene.export(output / "digital_twin.glb")
    collision_scene.export(output / "collision_scene.glb")

    trajectory = []
    if bool(export_cfg.get("write_camera_trajectory", True)):
        trajectory = camera_trajectory(
            NEXTGEN_ROOT / args.scene / "03_sfm/model",
            meters_per_unit,
        )
        (output / "camera_trajectory.json").write_text(json.dumps(trajectory, indent=2))

    scene_graph = {
        "schema": "namma.scene_graph.v1",
        "scene_id": args.scene,
        "root": "root",
        "coordinate_frame": "COLMAP global BA",
        "units": {
            "kind": "metres" if meters_per_unit is not None else "reconstruction_units",
            "meters_per_unit": meters_per_unit,
            "metric_calibrated": meters_per_unit is not None,
            "note": (
                "Uniform scale was explicitly supplied."
                if meters_per_unit is not None
                else "Monocular reconstruction scale is not guaranteed metric; provide one known real-world distance before physics requiring true metres."
            ),
        },
        "up_vector": (
            (np.asarray(layout["up_vector"], dtype=np.float64)).tolist()
            if meters_per_unit is None
            else (np.asarray(layout["up_vector"], dtype=np.float64)).tolist()
        ),
        "bounds": scene_bounds(visual_meshes),
        "nodes": scene_nodes,
        "camera_trajectory": "camera_trajectory.json" if trajectory else None,
        "initial_view": trajectory[0] if trajectory else None,
        "assets": {
            "visual_scene": "digital_twin.glb",
            "collision_scene": "collision_scene.glb",
        },
        "provenance": {
            "layout": "03_layout fitted observed architecture",
            "objects": "04_object_meshes observed masked-depth TSDF",
            "textures": "05_textures BA/depth-consistent observed RGB",
            "unseen_completion": "none",
        },
    }
    (output / "scene_graph.json").write_text(json.dumps(scene_graph, indent=2))

    summary = {
        "scene": args.scene,
        "complete": True,
        "nodes": len(scene_nodes),
        "architecture_nodes": sum(node["kind"] == "architecture" for node in scene_nodes),
        "object_nodes": sum(node["kind"] == "object" for node in scene_nodes),
        "visual_triangles": int(visual_triangles),
        "collision_triangles": int(collision_triangles),
        "visual_bounds": scene_bounds(visual_meshes),
        "metric_calibrated": meters_per_unit is not None,
        "meters_per_unit": meters_per_unit,
        "texture_rgb_assigned_fraction": float(texture_summary.get("rgb_assigned_fraction", 0.0)),
        "camera_poses": len(trajectory),
        "visual_scene": str(output / "digital_twin.glb"),
        "collision_scene": str(output / "collision_scene.glb"),
        "scene_graph": str(output / "scene_graph.json"),
        "unseen_completion": "none",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    (output / ".complete").write_text("ok\n")
    print("=== STAGE 6 COMPLETE ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
