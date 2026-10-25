#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from PIL import ExifTags, Image

from common import DATA_ROOT, env, env_bool, env_int, load_config, scene_root


Image.MAX_IMAGE_PIXELS = None


def _rational(value) -> float:
    return float(value[0]) / float(value[1]) if isinstance(value, tuple) else float(value)


def _gps_degrees(values, reference) -> float:
    degrees, minutes, seconds = (_rational(v) for v in values)
    result = degrees + minutes / 60.0 + seconds / 3600.0
    return -result if str(reference).upper() in {"S", "W"} else result


def read_capture_metadata(path: Path, image: Image.Image) -> dict:
    exif = image.getexif()
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    metadata = {}
    try:
        metadata["latitude"] = _gps_degrees(gps[2], gps[1])
        metadata["longitude"] = _gps_degrees(gps[4], gps[3])
        if 6 in gps:
            metadata["altitude_m"] = _rational(gps[6]) * (-1.0 if gps.get(5) == b"\x01" else 1.0)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass

    match = re.search(rb"<GPano:PoseHeadingDegrees>\s*([-+0-9.eE]+)\s*</GPano:PoseHeadingDegrees>", path.read_bytes())
    if match:
        try:
            metadata["heading_degrees"] = float(match.group(1))
        except ValueError:
            pass
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scene", nargs="?", default="hindoor_unibs")
    parser.add_argument("--overwrite", action="store_true", default=env_bool("NP_OVERWRITE"))
    args = parser.parse_args()

    cfg = load_config()
    dataset_scene = env("NP_DATASET_SCENE", cfg["dataset"]["scene"])
    max_width = env_int("NP_VIEWER_MAX_WIDTH", int(cfg["viewer"]["max_width"]))
    quality = env_int("NP_VIEWER_WEBP_QUALITY", int(cfg["viewer"]["webp_quality"]))
    src_dir = DATA_ROOT / "HIndoor-8K" / dataset_scene / "rgb"
    if not src_dir.is_dir():
        raise RuntimeError(f"Dataset is missing: {src_dir}. Run `fetch` first.")

    root = scene_root(args.scene)
    out = root / "01_panoramas"
    if out.exists() and args.overwrite:
        shutil.rmtree(out)
    source_out = out / "source"
    web_out = out / "web"
    source_out.mkdir(parents=True, exist_ok=True)
    web_out.mkdir(parents=True, exist_ok=True)

    images = sorted(
        p for p in src_dir.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if len(images) < 2:
        raise RuntimeError(f"Need at least two panoramas, found {len(images)}")

    stations = []
    for index, src in enumerate(images):
        station_id = f"station_{index:03d}"
        source_name = station_id + src.suffix.lower()
        source_path = source_out / source_name
        if not source_path.exists() or args.overwrite:
            shutil.copy2(src, source_path)

        with Image.open(src) as image:
            width, height = image.size
            if abs(width / max(height, 1) - 2.0) > 0.03:
                raise RuntimeError(f"Not an equirectangular 2:1 panorama: {src} ({width}x{height})")
            if width > max_width:
                new_h = max_width // 2
                image = image.resize((max_width, new_h), Image.Resampling.LANCZOS)
            web_path = web_out / f"{station_id}.webp"
            image.convert("RGB").save(web_path, "WEBP", quality=quality, method=6)
            capture_metadata = read_capture_metadata(src, image)

        stations.append({
            "id": station_id,
            "index": index,
            "original_name": src.name,
            "source": str(source_path),
            "web": str(web_path),
            "source_width": width,
            "source_height": height,
            **capture_metadata,
        })

    manifest = {
        "scene": args.scene,
        "dataset_scene": dataset_scene,
        "count": len(stations),
        "viewer_max_width": max_width,
        "webp_quality": quality,
        "stations": stations,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("=== NAMMA PANOGRAPH: PANORAMAS PREPARED ===")
    print(json.dumps({k: manifest[k] for k in ["scene", "count", "viewer_max_width", "webp_quality"]}, indent=2))


if __name__ == "__main__":
    main()
