#!/usr/bin/env python3
"""Prepare the static scene bundle used by the GitHub Pages workflow."""
import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCENES = ROOT / 'outputs/nextgen'
CONFIG = [
    ('frames_omega_test', 'Café Seating Area', 'A sunlit cafe interior with tables, chairs, and a window-side plant.', 10000),
    ('retail_competition_160', 'Furniture Showroom', 'A retail interior with lounge seating, display counters, and shelving.', 30000),
]

parser = argparse.ArgumentParser()
parser.add_argument('destination', type=Path)
args = parser.parse_args()
destination = args.destination.resolve()
destination.mkdir(parents=True, exist_ok=True)
catalog = []
for name, title, description, iteration in CONFIG:
    source = SCENES / name
    target = destination / 'scene' / name
    models = [
        {'id': 'optimized', 'label': 'Optimized Gaussians (SPZ)', 'asset': '09_web/scene.spz'},
        {'id': 'trained', 'label': 'Trained Gaussians (full detail)', 'asset': '09_web/scene_full_v2.spz'},
    ]
    if name == 'retail_competition_160':
        models.append({'id': 'sugar', 'label': 'SuGaR Final', 'asset': '08_mesh/sugar/final/sugar_textured_mesh.glb'})
    for model in models:
        file = source / model['asset']
        if not file.is_file():
            raise SystemExit(f'Missing asset: {file}')
        dest = target / model['asset']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, dest)
    render_dir = source / '07_renders/renders'
    renders = sorted(p for p in render_dir.iterdir() if p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'})
    samples = [renders[i] for i in sorted({0, len(renders) // 2, len(renders) - 1})] if renders else []
    for render in samples:
        dest = target / '07_renders/renders' / render.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(render, dest)
    thumbnail = f'scene/{name}/07_renders/renders/{samples[len(samples)//2].name}' if samples else None
    catalog.append({
        'name': name, 'display_name': title, 'description': description, 'iteration': iteration,
        'trained_bytes': (source / '09_web/scene_full_v2.spz').stat().st_size,
        'optimized_bytes': (source / '09_web/scene.spz').stat().st_size,
        'models': models, 'render_count': len(samples), 'renders': [p.name for p in samples], 'thumbnail': thumbnail,
    })
(destination / 'scenes.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
print(f'Prepared {len(catalog)} scenes in {destination}')
