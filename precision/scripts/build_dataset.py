#!/usr/bin/env python3
"""MVS geometry/depth and final cameras share one frame; no Sim(3) is used."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from mvs import read_colmap_array


def encode_inverse_depth(depth: np.ndarray, encode_scale: float) -> np.ndarray:
    result = np.zeros(depth.shape, dtype=np.float32)
    valid = np.isfinite(depth) & (depth > 1e-6)
    np.divide(encode_scale, depth, out=result, where=valid)
    return np.clip(np.rint(result), 0, 65535).astype(np.uint16)


def recenter(array: np.ndarray, cx: float, cy: float, interpolation: int) -> np.ndarray:
    h, w = array.shape[:2]
    # COLMAP pixel centers start at (0.5,0.5); Graphdeco assumes centered K.
    matrix = np.float32([[1, 0, w / 2 - cx], [0, 1, h / 2 - cy]])
    return cv2.warpAffine(array, matrix, (w, h), flags=interpolation,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def main() -> None:
    import pycolmap
    from plyfile import PlyData, PlyElement

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mvs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-points', type=int, default=400_000)
    args = parser.parse_args()
    if not 10_000 <= args.max_points <= 600_000:
        raise SystemExit('Initial Gaussian count must be between 10k and 600k.')
    if args.output.exists():
        raise SystemExit(f'Refusing to replace dataset: {args.output}')
    dense = args.mvs / 'dense'
    model = pycolmap.Reconstruction(str(dense / 'sparse'))
    ordered = sorted(model.images.values(), key=lambda im: im.name)
    inverse_samples = []
    for image in ordered:
        path = dense / 'stereo/depth_maps' / (image.name + '.geometric.bin')
        depth = read_colmap_array(path)
        good = depth[np.isfinite(depth) & (depth > 1e-6)]
        if not good.size:
            raise RuntimeError(f'No geometrically supported depth: {image.name}')
        # A stray tiny positive depth must not consume the entire uint16 range.
        stride = max(1, len(good) // 4096)
        inverse_samples.append(1.0 / good[::stride])
    maximum_inverse = float(np.percentile(np.concatenate(inverse_samples), 99.9))
    encode_scale = 65500.0 / maximum_inverse
    # Graphdeco loads uint16 depth with /65536; undo that normalization exactly.
    depth_scale = 65536.0 / encode_scale
    images_out, depths_out = args.output / 'images', args.output / 'depths'
    sparse_out = args.output / 'sparse/0'
    images_out.mkdir(parents=True)
    depths_out.mkdir()
    sparse_out.mkdir(parents=True)
    params, observations = {}, []
    # Each image receives its own centered camera in case several images shared
    # an input camera. Preserve sparse tracks while updating their pixel coords.
    camera_shifts = {}
    for camera_id, camera in model.cameras.items():
        if camera.model != pycolmap.CameraModelId.PINHOLE:
            raise RuntimeError(f'Expected rectified PINHOLE camera, got {camera.model}')
        K = camera.calibration_matrix()
        cx, cy = float(K[0, 2]), float(K[1, 2])
        camera_shifts[camera_id] = (cx, cy)
        camera.params = np.array([K[0, 0], K[1, 1], camera.width / 2, camera.height / 2])
    for image in ordered:
        camera = model.cameras[image.camera_id]
        cx, cy = camera_shifts[image.camera_id]
        rgb = cv2.imread(str(dense / 'images' / image.name), cv2.IMREAD_COLOR)
        if rgb is None or rgb.shape[:2] != (camera.height, camera.width):
            raise RuntimeError(f'MVS image/camera dimensions disagree: {image.name}')
        depth = read_colmap_array(dense / 'stereo/depth_maps' / (image.name + '.geometric.bin'))
        if depth.shape != rgb.shape[:2]:
            raise RuntimeError(f'MVS depth/RGB dimensions disagree: {image.name}')
        valid = np.isfinite(depth) & (depth > 1e-6)
        bounded = valid & (depth >= 1.0 / maximum_inverse)
        safe_depth = np.where(bounded, depth, 0.0)
        encoded = encode_inverse_depth(safe_depth, encode_scale)
        lost = float(np.count_nonzero(bounded & (encoded == 0)) / max(1, np.count_nonzero(bounded)))
        if lost > 0.01:
            raise RuntimeError(f'Depth range is too wide for safe uint16 priors in {image.name}: {lost:.1%} lost. Inspect scene scale/depth.')
        if np.count_nonzero(encoded) < depth.size * 0.001:
            raise RuntimeError(f'Too little valid encoded stereo depth: {image.name}')
        encoded = recenter(encoded, cx, cy, cv2.INTER_NEAREST)
        shifted = recenter(rgb, cx, cy, cv2.INTER_LINEAR)
        alpha = recenter(np.full(depth.shape, 255, dtype=np.uint8), cx, cy, cv2.INTER_NEAREST)
        rgba = np.dstack((shifted, alpha))
        output_name = Path(image.name).with_suffix('.png').name
        if Path(image.name).name != image.name:
            raise RuntimeError('Only flat image names are supported.')
        old_name = image.name
        if not cv2.imwrite(str(images_out / output_name), rgba):
            raise RuntimeError(f'Failed writing {output_name}')
        if not cv2.imwrite(str(depths_out / (Path(output_name).stem + '.png')), encoded):
            raise RuntimeError(f'Failed writing depth for {output_name}')
        image.name = output_name
        shift = np.array([camera.width / 2 - cx, camera.height / 2 - cy])
        for point in image.points2D:
            point.xy = np.asarray(point.xy) + shift
        params[Path(output_name).stem] = {'scale': depth_scale, 'offset': 0.0}
        observations.append({'image': output_name, 'source': old_name,
                             'depth_coverage': float(valid.mean()),
                             'encoded_depth_coverage': float(np.count_nonzero(encoded) / encoded.size),
                             'quantization_lost_fraction': lost,
                             'near_outlier_fraction': float(np.count_nonzero(valid & ~bounded) / max(1, np.count_nonzero(valid))),
                             'principal_point_shift': shift.tolist()})
    # TXT avoids binary format differences between CLI and Python dependencies.
    model.write_text(str(sparse_out))
    source = PlyData.read(str(dense / 'fused.ply'), mmap='r')['vertex'].data
    if len(source) < 1000:
        raise RuntimeError('Fused stereo cloud is too small; inspect MVS quality.')
    chosen = np.random.default_rng(0).choice(len(source), min(len(source), args.max_points), replace=False)
    vertices = np.zeros(len(chosen), dtype=[(n, t) for n, t in
        [('x', 'f4'), ('y', 'f4'), ('z', 'f4'), ('nx', 'f4'), ('ny', 'f4'), ('nz', 'f4'),
         ('red', 'u1'), ('green', 'u1'), ('blue', 'u1')]])
    for key in vertices.dtype.names:
        if key in source.dtype.names:
            vertices[key] = source[key][chosen]
    finite = np.ones(len(vertices), dtype=bool)
    for key in ('x', 'y', 'z'):
        finite &= np.isfinite(vertices[key])
    vertices = vertices[finite]
    if len(vertices) < 1000:
        raise RuntimeError('Too few finite MVS points.')
    PlyData([PlyElement.describe(vertices, 'vertex')], text=False).write(str(sparse_out / 'points3D.ply'))
    (sparse_out / 'depth_params.json').write_text(json.dumps(params, indent=2) + '\n')
    summary = {'geometry': 'COLMAP geometric stereo in final BA coordinates',
               'images': len(ordered), 'dense_points': len(source), 'initial_gaussians': len(vertices),
               'inverse_depth_encode_scale': encode_scale, 'graphdeco_depth_scale': depth_scale,
               'depth_masks': 'invalid stereo depth excluded by safe_train adapter',
               'cameras_recentered': True, 'views': observations}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
