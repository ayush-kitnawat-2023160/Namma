#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import pycolmap
import trimesh
import xatlas
import yaml
from PIL import Image
from scipy.ndimage import distance_transform_edt


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


def camera_pose(image) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pose = image.cam_from_world()
    matrix = np.asarray(pose.matrix(), dtype=np.float64)
    R = matrix[:, :3]
    t = matrix[:, 3]
    center = -R.T @ t
    return R, t, center


def pinhole_params(camera, width: int, height: int) -> tuple[float, float, float, float]:
    params = np.asarray(camera.params, dtype=np.float64)
    model = str(camera.model_name)
    if model == "PINHOLE":
        fx, fy, cx, cy = params[:4]
    elif model == "SIMPLE_PINHOLE":
        fx, cx, cy = params[:3]
        fy = fx
    else:
        raise RuntimeError(f"Stage 5 expects rectified PINHOLE cameras; got {model}")
    sx = width / float(camera.width)
    sy = height / float(camera.height)
    return float(fx * sx), float(fy * sy), float(cx * sx), float(cy * sy)


def project_points(points: np.ndarray, image, width: int, height: int):
    R, t, center = camera_pose(image)
    camera_xyz = points.astype(np.float64) @ R.T + t
    z = camera_xyz[:, 2]
    fx, fy, cx, cy = pinhole_params(image.camera, width, height)
    uv = np.full((len(points), 2), np.nan, dtype=np.float64)
    projectable = (
        np.isfinite(camera_xyz).all(axis=1)
        & np.isfinite(z)
        & (z > 1e-6)
    )
    if np.any(projectable):
        uv[projectable, 0] = (
            fx * camera_xyz[projectable, 0] / z[projectable] + cx
        )
        uv[projectable, 1] = (
            fy * camera_xyz[projectable, 1] / z[projectable] + cy
        )
    valid = (
        projectable
        & np.isfinite(uv).all(axis=1)
        & (uv[:, 0] >= 0)
        & (uv[:, 0] <= width - 1)
        & (uv[:, 1] >= 0)
        & (uv[:, 1] <= height - 1)
    )
    return uv, z, valid, center


def decode_depth(path: Path, encode_scale: float, metric_scale: float) -> np.ndarray:
    encoded = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if encoded is None:
        raise RuntimeError(f"Cannot read depth map: {path}")
    encoded = encoded.astype(np.float32)
    depth = np.zeros_like(encoded, dtype=np.float32)
    valid = encoded > 0
    depth[valid] = float(encode_scale) / encoded[valid]
    depth *= float(metric_scale)
    depth[~np.isfinite(depth)] = 0.0
    return depth


def sample_depth(depth: np.ndarray, uv: np.ndarray, image_width: int, image_height: int) -> np.ndarray:
    output = np.zeros(len(uv), dtype=depth.dtype)
    finite = np.isfinite(uv).all(axis=1)
    if not np.any(finite):
        return output
    x = np.rint(
        uv[finite, 0] * depth.shape[1] / max(1, image_width)
    ).astype(np.int32)
    y = np.rint(
        uv[finite, 1] * depth.shape[0] / max(1, image_height)
    ).astype(np.int32)
    x = np.clip(x, 0, depth.shape[1] - 1)
    y = np.clip(y, 0, depth.shape[0] - 1)
    output[finite] = depth[y, x]
    return output


def triangle_area_2d(triangles: np.ndarray) -> np.ndarray:
    a = triangles[:, 1] - triangles[:, 0]
    b = triangles[:, 2] - triangles[:, 0]
    return 0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])


def image_subsample(names: list[str], max_views: int) -> list[str]:
    names = sorted(dict.fromkeys(names))
    if max_views <= 0 or len(names) <= max_views:
        return names
    indices = np.linspace(0, len(names) - 1, max_views).round().astype(int)
    return [names[int(index)] for index in sorted(set(indices.tolist()))]


def object_mask_lookup(stage1: Path, object_metadata: dict) -> dict[str, Path]:
    output: dict[str, Path] = {}
    for observation in object_metadata.get("observations", []):
        image_name = str(observation["image"])
        local_id = int(observation["local_id"])
        record = json.loads(
            (stage1 / "instances" / f"{Path(image_name).stem}.json").read_text()
        )
        match = next(
            (
                item
                for item in record.get("instances", [])
                if int(item["local_id"]) == local_id
            ),
            None,
        )
        if match is not None:
            output[image_name] = stage1 / match["mask"]
    return output


def semantic_mask_lookup(stage1: Path, class_name: str, semantic_ids: dict[str, int]):
    class_id = semantic_ids.get(class_name)
    if class_id is None:
        return None

    def loader(image_name: str):
        semantic = cv2.imread(
            str(stage1 / "semantic" / f"{Path(image_name).stem}.png"),
            cv2.IMREAD_UNCHANGED,
        )
        if semantic is None:
            return None
        return semantic == int(class_id)

    return loader


def fixed_mask_loader(mask_paths: dict[str, Path]):
    def loader(image_name: str):
        path = mask_paths.get(image_name)
        if path is None:
            return None
        mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            return None
        return mask > 0

    return loader


def choose_source_views(
    mesh: trimesh.Trimesh,
    image_names: list[str],
    images_by_name: dict,
    frames_dir: Path,
    depth_dir: Path,
    encode_scale: float,
    metric_scale: float,
    mask_loader,
    min_incidence: float,
    min_projected_area: float,
    depth_tolerance_abs: float,
    depth_tolerance_rel: float,
) -> tuple[np.ndarray, np.ndarray, dict]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    face_centers = np.asarray(mesh.triangles_center, dtype=np.float64)
    face_normals = np.asarray(mesh.face_normals, dtype=np.float64)
    face_count = len(faces)
    best_score = np.full(face_count, -np.inf, dtype=np.float64)
    best_view = np.full(face_count, -1, dtype=np.int32)
    best_source_uv = np.zeros((face_count, 3, 2), dtype=np.float32)
    assigned_per_view = Counter()
    view_stats = {}

    for view_index, image_name in enumerate(image_names):
        camera = images_by_name.get(image_name)
        source_bgr = cv2.imread(str(frames_dir / image_name), cv2.IMREAD_COLOR)
        if camera is None or source_bgr is None:
            continue
        height, width = source_bgr.shape[:2]
        uv, z, vertex_valid, center = project_points(vertices, camera, width, height)
        tri_uv = uv[faces]
        tri_valid = vertex_valid[faces].all(axis=1)
        projected_area = triangle_area_2d(tri_uv)

        center_uv, center_z, center_valid, _ = project_points(
            face_centers, camera, width, height
        )
        mask = mask_loader(image_name) if mask_loader is not None else None
        mask_ok = np.ones(face_count, dtype=bool)
        if mask is not None:
            if mask.shape[:2] != (height, width):
                mask = cv2.resize(
                    mask.astype(np.uint8),
                    (width, height),
                    interpolation=cv2.INTER_NEAREST,
                ) > 0
            finite_centers = center_valid & np.isfinite(center_uv).all(axis=1)
            x = np.zeros(face_count, dtype=np.int32)
            y = np.zeros(face_count, dtype=np.int32)
            if np.any(finite_centers):
                x[finite_centers] = np.rint(center_uv[finite_centers, 0]).astype(np.int32)
                y[finite_centers] = np.rint(center_uv[finite_centers, 1]).astype(np.int32)
            inside = (
                finite_centers
                & (x >= 0)
                & (x < width)
                & (y >= 0)
                & (y < height)
            )
            mask_ok[:] = False
            ids = np.flatnonzero(inside)
            mask_ok[ids] = mask[y[ids], x[ids]]

        depth = decode_depth(
            depth_dir / f"{Path(image_name).stem}.png",
            encode_scale,
            metric_scale,
        )
        expected_depth = sample_depth(depth, center_uv, width, height)
        depth_tol = np.maximum(
            float(depth_tolerance_abs),
            float(depth_tolerance_rel) * np.maximum(expected_depth, 0.0),
        )
        depth_ok = (
            (expected_depth > 0)
            & np.isfinite(expected_depth)
            & (np.abs(center_z - expected_depth) <= depth_tol)
        )

        view_dir = center.reshape(1, 3) - face_centers
        view_dir /= np.maximum(np.linalg.norm(view_dir, axis=1, keepdims=True), 1e-12)
        incidence = np.abs(np.einsum("ij,ij->i", face_normals, view_dir))
        candidate = (
            tri_valid
            & center_valid
            & mask_ok
            & depth_ok
            & (incidence >= min_incidence)
            & (projected_area >= min_projected_area)
        )
        score = incidence * np.sqrt(np.maximum(projected_area, 0.0))
        update = candidate & (score > best_score)
        if np.any(update):
            best_score[update] = score[update]
            best_view[update] = view_index
            best_source_uv[update] = tri_uv[update].astype(np.float32)

        view_stats[image_name] = {
            "candidate_faces": int(np.count_nonzero(candidate)),
            "depth_consistent_faces": int(np.count_nonzero(depth_ok & tri_valid)),
            "mask_faces": int(np.count_nonzero(mask_ok & tri_valid)),
        }

    for view_index, name in enumerate(image_names):
        assigned_per_view[name] = int(np.count_nonzero(best_view == view_index))
    stats = {
        "faces": face_count,
        "assigned_faces": int(np.count_nonzero(best_view >= 0)),
        "unassigned_faces": int(np.count_nonzero(best_view < 0)),
        "assigned_fraction": float(np.count_nonzero(best_view >= 0) / max(1, face_count)),
        "assigned_per_view": dict(assigned_per_view),
        "view_stats": view_stats,
    }
    return best_view, best_source_uv, stats


def unwrap_mesh(mesh: trimesh.Trimesh):
    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.uint32)
    vmapping, indices, uvs = xatlas.parametrize(vertices, faces)
    return (
        np.asarray(vmapping, dtype=np.int64),
        np.asarray(indices, dtype=np.int64),
        np.asarray(uvs, dtype=np.float32),
    )


def sample_bilinear(image: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Vectorized bilinear sampling without OpenCV's SHRT_MAX map limit.

    cv2.remap rejects a sampling map when either map dimension reaches 32767.
    Large atlas triangles can easily generate more than that many texels in a
    single call, even though the source RGB image itself is small. Sampling
    directly with NumPy removes that artificial restriction.
    """
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    output = np.zeros((len(x), 3), dtype=np.uint8)
    finite = np.isfinite(x) & np.isfinite(y)
    if not np.any(finite):
        return output

    height, width = image.shape[:2]
    xf = np.clip(x[finite], 0.0, max(0.0, width - 1.0))
    yf = np.clip(y[finite], 0.0, max(0.0, height - 1.0))
    x0 = np.floor(xf).astype(np.int32)
    y0 = np.floor(yf).astype(np.int32)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    wx = (xf - x0).astype(np.float32)
    wy = (yf - y0).astype(np.float32)

    c00 = image[y0, x0].astype(np.float32)
    c10 = image[y0, x1].astype(np.float32)
    c01 = image[y1, x0].astype(np.float32)
    c11 = image[y1, x1].astype(np.float32)
    top = c00 * (1.0 - wx[:, None]) + c10 * wx[:, None]
    bottom = c01 * (1.0 - wx[:, None]) + c11 * wx[:, None]
    sampled = top * (1.0 - wy[:, None]) + bottom * wy[:, None]
    output[finite] = np.clip(sampled, 0, 255).astype(np.uint8)
    return output


def rasterize_triangle(
    texture: np.ndarray,
    occupied: np.ndarray,
    atlas_tri: np.ndarray,
    source_tri: np.ndarray | None,
    source_image: np.ndarray | None,
    fallback_colors: np.ndarray,
) -> int:
    h, w = occupied.shape
    min_x = max(0, int(math.floor(np.min(atlas_tri[:, 0]))))
    max_x = min(w - 1, int(math.ceil(np.max(atlas_tri[:, 0]))))
    min_y = max(0, int(math.floor(np.min(atlas_tri[:, 1]))))
    max_y = min(h - 1, int(math.ceil(np.max(atlas_tri[:, 1]))))
    if min_x > max_x or min_y > max_y:
        return 0

    xs, ys = np.meshgrid(
        np.arange(min_x, max_x + 1, dtype=np.float32) + 0.5,
        np.arange(min_y, max_y + 1, dtype=np.float32) + 0.5,
    )
    p = np.stack([xs, ys], axis=-1)
    a, b, c = atlas_tri.astype(np.float32)
    v0 = b - a
    v1 = c - a
    v2 = p - a
    den = v0[0] * v1[1] - v1[0] * v0[1]
    if abs(float(den)) < 1e-8:
        return 0
    wb = (v2[..., 0] * v1[1] - v1[0] * v2[..., 1]) / den
    wc = (v0[0] * v2[..., 1] - v2[..., 0] * v0[1]) / den
    wa = 1.0 - wb - wc
    inside = (wa >= -1e-4) & (wb >= -1e-4) & (wc >= -1e-4)
    if not np.any(inside):
        return 0

    weights = np.stack([wa[inside], wb[inside], wc[inside]], axis=1)
    yy, xx = np.nonzero(inside)
    yy = yy + min_y
    xx = xx + min_x
    if source_tri is not None and source_image is not None:
        source_xy = weights @ source_tri.astype(np.float32)
        colors = sample_bilinear(source_image, source_xy[:, 0], source_xy[:, 1])
    else:
        colors = np.clip(weights @ fallback_colors.astype(np.float32), 0, 255).astype(np.uint8)
    texture[yy, xx] = colors
    occupied[yy, xx] = True
    return int(len(xx))


def pad_texture(texture: np.ndarray, occupied: np.ndarray, padding: int) -> np.ndarray:
    if padding <= 0 or not np.any(occupied):
        return texture
    distance, indices = distance_transform_edt(~occupied, return_indices=True)
    fill = (~occupied) & (distance <= int(padding))
    if np.any(fill):
        texture[fill] = texture[indices[0][fill], indices[1][fill]]
    return texture


def bake_mesh(
    mesh: trimesh.Trimesh,
    image_names: list[str],
    images_by_name: dict,
    frames_dir: Path,
    depth_dir: Path,
    encode_scale: float,
    metric_scale: float,
    mask_loader,
    texture_resolution: int,
    min_incidence: float,
    min_projected_area: float,
    depth_tolerance_abs: float,
    depth_tolerance_rel: float,
    atlas_padding: int,
    fallback_color: tuple[int, int, int],
    roughness_factor: float,
) -> tuple[trimesh.Trimesh, np.ndarray, dict]:
    mesh = mesh.copy()
    if not mesh.is_empty:
        mesh.fix_normals(multibody=True)
    best_view, best_source_uv, assignment = choose_source_views(
        mesh,
        image_names,
        images_by_name,
        frames_dir,
        depth_dir,
        encode_scale,
        metric_scale,
        mask_loader,
        min_incidence,
        min_projected_area,
        depth_tolerance_abs,
        depth_tolerance_rel,
    )
    vmapping, faces_uv, uvs = unwrap_mesh(mesh)
    new_vertices = np.asarray(mesh.vertices)[vmapping]
    atlas_xy = np.empty_like(uvs, dtype=np.float32)
    atlas_xy[:, 0] = uvs[:, 0] * (texture_resolution - 1)
    atlas_xy[:, 1] = (1.0 - uvs[:, 1]) * (texture_resolution - 1)

    vertex_colors = np.full((len(mesh.vertices), 3), fallback_color, dtype=np.uint8)
    if getattr(mesh.visual, "kind", None) == "vertex":
        raw = np.asarray(mesh.visual.vertex_colors)
        if len(raw) == len(mesh.vertices):
            vertex_colors = raw[:, :3].astype(np.uint8)
    new_colors = vertex_colors[vmapping]

    texture = np.zeros((texture_resolution, texture_resolution, 3), dtype=np.uint8)
    occupied = np.zeros((texture_resolution, texture_resolution), dtype=bool)
    source_cache: dict[str, np.ndarray] = {}
    raster_pixels = 0
    fallback_faces = 0

    for face_index, uv_face in enumerate(faces_uv):
        atlas_tri = atlas_xy[uv_face]
        view_index = int(best_view[face_index])
        if view_index >= 0:
            image_name = image_names[view_index]
            if image_name not in source_cache:
                bgr = cv2.imread(str(frames_dir / image_name), cv2.IMREAD_COLOR)
                if bgr is None:
                    source_cache[image_name] = None
                else:
                    source_cache[image_name] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            source_image = source_cache[image_name]
            source_tri = best_source_uv[face_index]
        else:
            source_image = None
            source_tri = None
            fallback_faces += 1
        raster_pixels += rasterize_triangle(
            texture,
            occupied,
            atlas_tri,
            source_tri,
            source_image,
            new_colors[uv_face],
        )

    texture = pad_texture(texture, occupied, atlas_padding)
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(texture),
        metallicFactor=0.0,
        roughnessFactor=float(roughness_factor),
    )
    visual = trimesh.visual.texture.TextureVisuals(uv=uvs, material=material)
    textured = trimesh.Trimesh(
        vertices=new_vertices,
        faces=faces_uv,
        visual=visual,
        process=False,
    )
    stats = {
        **assignment,
        "texture_resolution": int(texture_resolution),
        "atlas_occupied_pixels": int(np.count_nonzero(occupied)),
        "atlas_occupied_fraction": float(np.count_nonzero(occupied) / occupied.size),
        "raster_pixels_written": int(raster_pixels),
        "fallback_faces": int(fallback_faces),
        "uv_vertices": int(len(new_vertices)),
    }
    return textured, texture, stats


def save_textured_asset(
    output_dir: Path,
    textured: trimesh.Trimesh,
    texture: np.ndarray,
    metadata: dict,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(texture).save(output_dir / "texture.png")
    textured.export(output_dir / "textured_mesh.glb")
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Namma Stage 5: observed RGB texture baking")
    parser.add_argument("scene")
    parser.add_argument("--overwrite", action="store_true", default=env_bool("NS_TEXTURE_OVERWRITE"))
    args = parser.parse_args()

    cfg = load_config()["texturing"]
    object_resolution = int(os.environ.get("NS_TEXTURE_RESOLUTION", cfg["object_texture_resolution"]))
    architecture_resolution = int(
        os.environ.get("NS_ARCH_TEXTURE_RESOLUTION", cfg["architecture_texture_resolution"])
    )
    min_incidence = float(os.environ.get("NS_TEXTURE_MIN_INCIDENCE", cfg["min_incidence"]))
    min_area = float(os.environ.get("NS_TEXTURE_MIN_AREA", cfg["min_projected_area"]))
    object_depth_abs = float(os.environ.get("NS_TEXTURE_DEPTH_TOLERANCE", cfg["depth_tolerance_abs"]))
    padding = int(os.environ.get("NS_TEXTURE_PADDING", cfg["atlas_padding"]))

    base = SEMANTIC_ROOT / args.scene
    stage1 = base / "01_segmentation"
    stage3 = base / "03_layout"
    stage4 = base / "04_object_meshes"
    for required in [stage1 / "summary.json", stage3 / "summary.json", stage4 / "summary.json"]:
        if not required.is_file():
            raise RuntimeError(f"Stage 5 prerequisite missing: {required}")

    output = base / "05_textures"
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Texture output already exists: {output}. Use --overwrite to replace it.")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    nextgen = NEXTGEN_ROOT / args.scene
    vggt_summary = json.loads((nextgen / "02_vggt/summary.json").read_text())
    alignment = json.loads((nextgen / "04_gs_dataset/vggt_to_colmap_alignment.json").read_text())
    encode_scale = float(vggt_summary["inverse_depth_encode_scale"])
    metric_scale = float(alignment["scale"])
    reconstruction = pycolmap.Reconstruction(str(nextgen / "03_sfm/model"))
    images_by_name = {image.name: image for image in reconstruction.images.values()}
    all_images = sorted(images_by_name)
    frames_dir = nextgen / "01_frames/images"
    depth_dir = nextgen / "02_vggt/depths"

    semantic_labels = json.loads((stage1 / "labels.json").read_text())
    semantic_ids = {
        str(name): int(value) for name, value in semantic_labels["semantic_ids"].items()
    }

    scene = trimesh.Scene()
    records = []
    architecture_count = 0
    object_count = 0
    assigned_faces_total = 0
    faces_total = 0

    print("=== NAMMA STAGE 5: RGB TEXTURE BAKING ===")
    print(f"Scene:                    {args.scene}")
    print(f"Object texture:           {object_resolution}x{object_resolution}")
    print(f"Architecture texture:     {architecture_resolution}x{architecture_resolution}")

    architecture_specs = []
    for mesh_path in sorted((stage3 / "meshes").glob("*.ply")):
        stem = mesh_path.stem
        if stem == "floor":
            class_name = "floor"
        elif stem.startswith("wall_"):
            class_name = "wall"
        elif stem.startswith("pillar_"):
            class_name = "pillar"
        elif stem == "ceiling":
            class_name = "ceiling"
        else:
            continue
        architecture_specs.append((stem, class_name, mesh_path))

    for asset_id, class_name, mesh_path in architecture_specs:
        mesh = trimesh.load(mesh_path, force="mesh", process=False)
        names = image_subsample(all_images, int(cfg["max_architecture_views"]))
        mask_loader = semantic_mask_lookup(stage1, class_name, semantic_ids)
        textured, texture, stats = bake_mesh(
            mesh,
            names,
            images_by_name,
            frames_dir,
            depth_dir,
            encode_scale,
            metric_scale,
            mask_loader,
            architecture_resolution,
            min_incidence,
            min_area,
            float(cfg["architecture_depth_tolerance_abs"]),
            float(cfg["architecture_depth_tolerance_rel"]),
            padding,
            tuple(int(v) for v in cfg["fallback_color"]),
            float(cfg["roughness_factor"]),
        )
        record = {
            "asset_id": asset_id,
            "kind": "architecture",
            "class": class_name,
            "source_mesh": str(mesh_path),
            "provenance": "observed RGB projected with BA cameras and VGGT depth consistency",
            **stats,
        }
        out = output / "architecture" / asset_id
        save_textured_asset(out, textured, texture, record)
        scene.add_geometry(textured, geom_name=asset_id, node_name=asset_id)
        records.append(record)
        architecture_count += 1
        assigned_faces_total += stats["assigned_faces"]
        faces_total += stats["faces"]
        print(
            f"[texture] {asset_id}: {stats['assigned_faces']}/{stats['faces']} "
            f"faces sourced from RGB"
        )

    object_dirs = sorted(
        path for path in (stage4 / "objects").iterdir() if path.is_dir()
    )
    for object_dir in object_dirs:
        metadata_file = object_dir / "metadata.json"
        mesh_file = object_dir / "observed_mesh.ply"
        if not metadata_file.is_file() or not mesh_file.is_file():
            continue
        object_metadata = json.loads(metadata_file.read_text())
        mesh = trimesh.load(mesh_file, force="mesh", process=False)
        mask_paths = object_mask_lookup(stage1, object_metadata)
        names = image_subsample(
            sorted(mask_paths),
            int(cfg["max_object_views"]),
        )
        if not names:
            names = image_subsample(
                [str(value) for value in object_metadata.get("source_views", [])],
                int(cfg["max_object_views"]),
            )
        textured, texture, stats = bake_mesh(
            mesh,
            names,
            images_by_name,
            frames_dir,
            depth_dir,
            encode_scale,
            metric_scale,
            fixed_mask_loader(mask_paths),
            object_resolution,
            min_incidence,
            min_area,
            object_depth_abs,
            float(cfg["depth_tolerance_rel"]),
            padding,
            tuple(int(v) for v in cfg["fallback_color"]),
            float(cfg["roughness_factor"]),
        )
        record = {
            "asset_id": object_dir.name,
            "kind": "object",
            "class": object_metadata.get("class"),
            "source_mesh": str(mesh_file),
            "source_views": names,
            "provenance": "observed RGB projected through Stage-1 instance masks with BA cameras and VGGT depth consistency",
            **stats,
        }
        out = output / "objects" / object_dir.name
        save_textured_asset(out, textured, texture, record)
        scene.add_geometry(textured, geom_name=object_dir.name, node_name=object_dir.name)
        records.append(record)
        object_count += 1
        assigned_faces_total += stats["assigned_faces"]
        faces_total += stats["faces"]
        print(
            f"[texture] {object_dir.name}: {stats['assigned_faces']}/{stats['faces']} "
            f"faces sourced from RGB"
        )

    if scene.geometry:
        scene.export(output / "textured_scene_preview.glb")

    summary = {
        "scene": args.scene,
        "complete": True,
        "architecture_assets": architecture_count,
        "object_assets": object_count,
        "total_assets": architecture_count + object_count,
        "faces": int(faces_total),
        "rgb_assigned_faces": int(assigned_faces_total),
        "rgb_assigned_fraction": float(assigned_faces_total / max(1, faces_total)),
        "object_texture_resolution": object_resolution,
        "architecture_texture_resolution": architecture_resolution,
        "atlas_padding": padding,
        "depth_source": "decoded VGGT-Omega depth scaled into COLMAP BA coordinates",
        "camera_source": "COLMAP global-BA model",
        "completion": "none",
        "preview": str(output / "textured_scene_preview.glb"),
    }
    (output / "assets.json").write_text(json.dumps(records, indent=2))
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    (output / ".complete").write_text("ok\n")
    print("=== STAGE 5 COMPLETE ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
