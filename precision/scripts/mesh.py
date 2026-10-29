#!/usr/bin/env python3
"""Fuse geometric stereo depths into an observed-surface TSDF mesh."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from mvs import read_colmap_array


def main() -> None:
    import open3d as o3d
    import pycolmap

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mvs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--voxel-divisor', type=int, default=512)
    args = parser.parse_args()
    if not 128 <= args.voxel_divisor <= 768:
        raise SystemExit('Voxel divisor must be 128..768 (scale-relative memory guard).')
    if args.output.exists():
        raise SystemExit(f'Refusing to replace mesh: {args.output}')
    dense = args.mvs / 'dense'
    model = pycolmap.Reconstruction(str(dense / 'sparse'))
    ordered = sorted(model.images.values(), key=lambda im: im.name)
    samples = []
    for image in ordered:
        depth = read_colmap_array(dense / 'stereo/depth_maps' / (image.name + '.geometric.bin'))
        sample = depth[::16, ::16].reshape(-1)
        samples.append(sample[np.isfinite(sample) & (sample > 1e-6)])
    positive = np.concatenate(samples)
    if not len(positive):
        raise RuntimeError('No supported stereo depth for TSDF.')
    median_depth = float(np.median(positive))
    voxel = median_depth / args.voxel_divisor
    depth_trunc = float(np.percentile(positive, 99))
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel, sdf_trunc=4 * voxel,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)
    args.output.mkdir(parents=True)
    views = []
    for image in ordered:
        camera = model.cameras[image.camera_id]
        K = camera.calibration_matrix()
        depth = np.array(read_colmap_array(dense / 'stereo/depth_maps' / (image.name + '.geometric.bin')), copy=True)
        depth[~np.isfinite(depth) | (depth <= 0) | (depth > depth_trunc)] = 0
        # Stereo filtering has already required agreement with other cameras.
        # Remove the immediate border of invalid depth before TSDF integration.
        valid = cv2.erode((depth > 0).astype(np.uint8), np.ones((3, 3), np.uint8))
        depth[valid == 0] = 0
        bgr = cv2.imread(str(dense / 'images' / image.name))
        if bgr is None or bgr.shape[:2] != depth.shape:
            raise RuntimeError(f'Stereo RGB/depth size mismatch for {image.name}')
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), o3d.geometry.Image(depth.astype(np.float32)),
            depth_scale=1.0, depth_trunc=depth_trunc, convert_rgb_to_intensity=False)
        intrinsic = o3d.camera.PinholeCameraIntrinsic(camera.width, camera.height,
            K[0, 0], K[1, 1], K[0, 2] - 0.5, K[1, 2] - 0.5)
        pose = image.cam_from_world
        pose = pose() if callable(pose) else pose
        extrinsic = np.eye(4)
        extrinsic[:3, :] = pose.matrix()
        volume.integrate(rgbd, intrinsic, extrinsic)
        views.append({'image': image.name, 'depth_coverage': float((depth > 0).mean())})
        print(f'TSDF {image.name}: observed depth coverage {views[-1]["depth_coverage"]:.1%}', flush=True)
    mesh = volume.extract_triangle_mesh()
    mesh.remove_duplicated_vertices()
    mesh.remove_duplicated_triangles()
    mesh.remove_degenerate_triangles()
    mesh.remove_unreferenced_vertices()
    mesh.compute_vertex_normals()
    if len(mesh.triangles) < 100:
        raise RuntimeError('Insufficient observed surface for a mesh.')
    if not o3d.io.write_triangle_mesh(str(args.output / 'observed_surface.ply'), mesh):
        raise RuntimeError('Failed writing observed surface mesh.')
    # A bounded static collision mesh. This does not assign invented masses,
    # closed object backsides, metric scale, or a simulated physical response.
    collision = mesh.simplify_quadric_decimation(min(100_000, len(mesh.triangles)))
    if not o3d.io.write_triangle_mesh(str(args.output / 'collision_static.ply'), collision):
        raise RuntimeError('Failed writing collision mesh.')
    report = {'coordinate_frame': 'final COLMAP BA', 'metric_calibrated': False,
        'units': 'arbitrary monocular reconstruction units', 'unseen_surface_completion': 'none',
        'voxel_size': voxel, 'depth_truncation': depth_trunc,
        'vertices': len(mesh.vertices), 'triangles': len(mesh.triangles),
        'collision_triangles': len(collision.triangles),
        'collision_usage': 'static triangle collider; set measured scale before gravity simulation', 'views': views}
    (args.output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
