#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from einops import rearrange, repeat
from PIL import Image
from torchvision.transforms.functional import to_tensor

from common import CHECKPOINT_ROOT, env, env_bool, env_float, env_int, load_config, load_graph, load_poses, scene_root

PAN_SPLAT_ROOT = Path('/opt/PanSplat')
sys.path.insert(0, str(PAN_SPLAT_ROOT))
os.chdir(PAN_SPLAT_ROOT)

import hydra
from hydra import compose, initialize_config_dir
from lightning_fabric.utilities.apply_func import apply_to_collection
from omegaconf import OmegaConf
from src.config import load_typed_root_config
from src.global_cfg import set_cfg
from src.model.decoder import get_decoder
from src.model.encoder import get_encoder
from src.model.model_wrapper import ModelWrapper
from src.model.encoder.unifuse.datasets.util import Equirec2Cube


def pose_map(scene: str) -> dict[str, np.ndarray]:
    out = {}
    for record in load_poses(scene):
        out[record['id']] = np.asarray(record['camera_to_world'], dtype=np.float32)
    return out


def resolve_edge(scene: str, a: str, b: str) -> tuple[dict, dict]:
    graph = load_graph(scene)
    nodes = {n['id']: n for n in graph['nodes']}
    if a not in nodes or b not in nodes:
        raise RuntimeError(f'Unknown node(s): {a}, {b}')
    valid = {tuple(sorted((e['a'], e['b']))) for e in graph['edges']}
    if tuple(sorted((a, b))) not in valid:
        raise RuntimeError(f'{a} -> {b} is not a PanoGraph edge')
    return nodes[a], nodes[b]


def image_tensor(path: Path, h: int, w: int) -> torch.Tensor:
    im = Image.open(path).convert('RGB').resize((w, h), Image.Resampling.LANCZOS)
    return to_tensor(im)


def build_context(images: list[torch.Tensor], c2w: torch.Tensor, near: float, far: float) -> dict:
    imgs = torch.stack(images, dim=0)
    mono = F.interpolate(imgs, size=(256, 512), mode='bilinear', align_corners=False)
    mono = F.interpolate(mono, size=(512, 1024), mode='bilinear', align_corners=False)
    e2c = Equirec2Cube(512, 1024, 256)
    cubes = []
    for img in mono:
        np_img = rearrange(img.cpu().numpy(), 'c h w -> h w c')
        cube = e2c.run(np_img)
        cubes.append(torch.from_numpy(rearrange(cube, 'h w c -> c h w')).float())
    cube = torch.stack(cubes)
    intr = torch.eye(3, dtype=torch.float32)
    intr[0,0], intr[1,1], intr[0,2], intr[1,2] = 0.25, 0.5, 0.5, 0.5
    return {
        'image': imgs.unsqueeze(0),
        'mono_image': mono.unsqueeze(0),
        'cube_image': cube.unsqueeze(0),
        'extrinsics': c2w.unsqueeze(0),
        'intrinsics': repeat(intr, 'i j -> b v i j', b=1, v=2),
        'near': torch.full((1,2), near, dtype=torch.float32),
        'far': torch.full((1,2), far, dtype=torch.float32),
        'index': torch.tensor([[0,1]], dtype=torch.int64),
    }


def normalize_pair(c0: np.ndarray, c1: np.ndarray, normalize_baseline: bool) -> torch.Tensor:
    c0 = np.asarray(c0, dtype=np.float32)
    c1 = np.asarray(c1, dtype=np.float32)
    rel = np.linalg.inv(c0) @ c1
    a = np.eye(4, dtype=np.float32)
    b = rel.astype(np.float32)
    baseline = float(np.linalg.norm(b[:3,3]))
    if baseline < 1e-6:
        raise RuntimeError('Degenerate PanSplat edge: near-zero baseline')
    if normalize_baseline:
        b[:3,3] /= baseline
    return torch.from_numpy(np.stack([a,b])), baseline


def build_model(profile: str, checkpoint: Path):
    experiment = 'pansplat-512' if profile == '512' else 'pansplat-2048'
    with initialize_config_dir(version_base=None, config_dir=str(PAN_SPLAT_ROOT / 'config')):
        cfg_dict = compose(config_name='main', overrides=[
            f'+experiment={experiment}',
            'mode=predict',
            f'model.weights_path={checkpoint}',
            'wandb.mode=disabled',
            'data_loader.test.num_workers=0',
        ])
    cfg = load_typed_root_config(cfg_dict)
    set_cfg(cfg_dict)
    encoder, vis = get_encoder(cfg.model.encoder)
    decoder = get_decoder(cfg.model.decoder, cfg.dataset)
    wrapper = ModelWrapper(cfg.optimizer, cfg.test, cfg.train, cfg.val, cfg.predict,
                           encoder, vis, decoder, [], None, cfg.model.weights_path, cfg.model.wo_defbp2)
    wrapper.eval().cuda()
    return wrapper, cfg


def ffmpeg_pipe(path: Path, width: int, height: int, fps: int, crf: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'ffmpeg','-y','-loglevel','error',
        '-f','rawvideo','-pix_fmt','rgb24','-s',f'{width}x{height}','-r',str(fps),'-i','-',
        '-an','-c:v','libvpx-vp9','-deadline','good','-cpu-used','2','-crf',str(crf),'-b:v','0',
        '-pix_fmt','yuv420p',str(path)
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def main() -> None:
    p = argparse.ArgumentParser(description='Render one PanoGraph edge with PanSplat')
    p.add_argument('scene')
    p.add_argument('a')
    p.add_argument('b')
    p.add_argument('--profile', choices=['512','4k'], default=env('PS_PROFILE','512'))
    p.add_argument('--frames', type=int, default=None)
    args = p.parse_args()

    cfg_all = load_config()
    rcfg, scfg = cfg_all['render'], cfg_all['safety']
    node_a, node_b = resolve_edge(args.scene, args.a, args.b)
    poses = pose_map(args.scene)
    if args.a not in poses or args.b not in poses:
        raise RuntimeError('PanoVGGT poses are required; fallback-circle graphs are not accepted for GS transitions.')

    profile = args.profile
    ckpt = Path(os.environ.get('PS_CHECKPOINT', CHECKPOINT_ROOT / 'pansplat' / ('pansplat-512.ckpt' if profile=='512' else 'pansplat-2048.ckpt')))
    if not ckpt.is_file():
        raise RuntimeError(f'Missing checkpoint {ckpt}; run `./run_pansplat.sh models {profile}` first.')
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA required.')
    free, total = torch.cuda.mem_get_info(0)
    free_mb = int(free // 2**20)
    minimum = env_int('PS_GPU_MIN_FREE_MB', int(scfg[f'min_free_vram_mb_{profile}']))
    if free_mb < minimum:
        raise RuntimeError(f'GPU guard: {free_mb} MiB free < required {minimum} MiB')
    fraction = env_float('PS_GPU_MEMORY_FRACTION', float(scfg[f'gpu_memory_fraction_{profile}']))
    if fraction > 0:
        if not 0.2 <= fraction <= 0.98:
            raise RuntimeError('PS_GPU_MEMORY_FRACTION must be 0 or [0.2,0.98].')
        torch.cuda.set_per_process_memory_fraction(fraction, 0)

    h,w = ((512,1024) if profile=='512' else (2048,4096))
    frames = args.frames or env_int('PS_FRAMES', int(rcfg['frames']))
    fps = env_int('PS_FPS', int(rcfg['fps']))
    near, far = float(rcfg['near']), float(rcfg['far'])
    normalize = env_bool('PS_NORMALIZE_BASELINE', bool(rcfg['normalize_baseline']))
    render_batch = env_int('PS_RENDER_BATCH', int(rcfg[f'render_batch_{profile}']))
    crf = env_int('PS_WEBM_CRF', int(rcfg['webm_crf']))

    img_a = Path(node_a['web_panorama'])
    img_b = Path(node_b['web_panorama'])
    if not img_a.is_file() or not img_b.is_file():
        raise RuntimeError(f'Missing prepared panoramas: {img_a}, {img_b}')
    c2w, raw_baseline = normalize_pair(poses[args.a], poses[args.b], normalize)

    out_dir = scene_root(args.scene) / '05_pansplat' / 'transitions'
    name = f'{args.a}__{args.b}'
    out_file = out_dir / f'{name}.webm'
    meta_file = out_dir / f'{name}.json'

    print('=== NAMMA PANOSPLAT: EDGE RENDER ===')
    print(json.dumps({
        'scene': args.scene, 'edge': [args.a,args.b], 'profile': profile,
        'resolution': [h,w], 'frames': frames, 'fps': fps,
        'raw_baseline': raw_baseline, 'normalize_baseline': normalize,
        'free_vram_mb': free_mb, 'torch_fraction': fraction,
    }, indent=2))

    try:
        wrapper, typed_cfg = build_model(profile, ckpt)
        context = build_context([image_tensor(img_a,h,w), image_tensor(img_b,h,w)], c2w, near, far)
        device = wrapper.device
        context = apply_to_collection(context, torch.Tensor, lambda x: x.to(device))
        torch.cuda.reset_peak_memory_stats(0)
        with torch.inference_mode():
            gaussians = wrapper.encoder(context, inference=True)['gaussians']['gaussians']
        ctx_ext = context['extrinsics']

        def render_directed(src_idx: int, dst_idx: int, video_path: Path) -> None:
            process = ffmpeg_pipe(video_path, w, h, fps, crf)
            try:
                ts = torch.linspace(0, 1, frames, device=device)
                ts = (torch.cos(torch.pi * (ts + 1)) + 1) / 2
                src_pose = ctx_ext[:, src_idx]
                dst_pose = ctx_ext[:, dst_idx]
                src_center = src_pose[:, :3, 3]
                dst_center = dst_pose[:, :3, 3]
                for start in range(0, frames, render_batch):
                    t = ts[start:start + render_batch]
                    vb = t.numel()
                    ext = repeat(src_pose, 'b i j -> b v i j', v=vb).clone()
                    centers = src_center[:, None] * (1 - t[None, :, None]) + dst_center[:, None] * t[None, :, None]
                    ext[:, :, :3, 3] = centers
                    # Keep the source station's orientation fixed while the camera center
                    # moves. This makes the equirectangular video align with the source
                    # panorama sphere in SPHR instead of spinning during playback.
                    ext[:, :, :3, :3] = src_pose[:, None, :3, :3]
                    n = repeat(context['near'][:, 0], 'b -> b v', v=vb)
                    f = repeat(context['far'][:, 0], 'b -> b v', v=vb)
                    with torch.inference_mode():
                        rendered = wrapper.decoder.render_pano(
                            gaussians, ext, n, f, None, cpu=True, context_extrinsics=ctx_ext
                        )['color'][0]
                    u8 = rendered.clamp(0, 1).mul(255).byte().permute(0, 2, 3, 1).contiguous().numpy()
                    assert process.stdin is not None
                    for frame in u8:
                        process.stdin.write(frame.tobytes())
                    del rendered, u8, ext, n, f, centers
                    torch.cuda.empty_cache()
            finally:
                if process.stdin:
                    process.stdin.close()
                code = process.wait()
                if code != 0:
                    raise RuntimeError(f'ffmpeg failed with exit code {code}')

        reverse_file = out_dir / f'{args.b}__{args.a}.webm'
        render_directed(0, 1, out_file)
        render_directed(1, 0, reverse_file)

        peak_mb = int(torch.cuda.max_memory_allocated(0) // 2**20)
        meta = {
            'scene': args.scene, 'a': args.a, 'b': args.b,
            'profile': profile, 'resolution': [h,w], 'frames': frames, 'fps': fps,
            'source_a': str(img_a), 'source_b': str(img_b),
            'raw_baseline': raw_baseline, 'normalized_baseline': normalize,
            'checkpoint': str(ckpt), 'peak_torch_allocated_mb': peak_mb,
            'video_forward': str(out_file), 'video_reverse': str(reverse_file), 'complete': True,
        }
        meta_file.write_text(json.dumps(meta, indent=2))
        print(json.dumps(meta, indent=2))
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.write_text(json.dumps({'scene':args.scene,'a':args.a,'b':args.b,'complete':False,'error':'CUDA OOM'},indent=2))
        raise RuntimeError('PanSplat CUDA OOM; this edge process stopped without increasing the VRAM limit.') from exc
    finally:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


if __name__ == '__main__':
    main()
