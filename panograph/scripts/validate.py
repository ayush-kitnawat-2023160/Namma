#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from common import DATA_ROOT, load_config, scene_root


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a complete Namma PanoGraph package")
    parser.add_argument("scene", nargs="?", default="hindoor_unibs")
    args = parser.parse_args()
    cfg = load_config()
    dataset_scene = str(os.environ.get("NP_DATASET_SCENE", cfg["dataset"]["scene"]))
    dataset_root = DATA_ROOT / "HIndoor-8K" / dataset_scene
    rgb = sorted((dataset_root / "rgb").glob("*")) if (dataset_root / "rgb").is_dir() else []
    depth_dir = dataset_root / "depth"

    root = scene_root(args.scene)
    manifest_file = root / "01_panoramas" / "manifest.json"
    graph_file = root / "03_graph" / "graph.json"
    package = root / "04_sphr"
    bootstrap_file = package / "bootstrap.json"
    require(manifest_file.is_file(), f"Missing panorama manifest: {manifest_file}")
    require(graph_file.is_file(), f"Missing graph: {graph_file}")
    require(bootstrap_file.is_file(), f"Missing SPHR bootstrap: {bootstrap_file}")

    manifest = json.loads(manifest_file.read_text())
    graph = json.loads(graph_file.read_text())
    bootstrap = json.loads(bootstrap_file.read_text())
    expected_count = int(manifest.get("count", 0))
    require(expected_count >= 2, f"Prepared manifest must contain at least 2 stations, found {expected_count}")
    require(len(rgb) == expected_count, f"Dataset RGB count ({len(rgb)}) does not match prepared stations ({expected_count})")
    require(not depth_dir.exists(), f"RGB-only policy violated: depth directory exists: {depth_dir}")
    require(len(graph.get("nodes", [])) == expected_count, f"Graph does not contain {expected_count} nodes")
    require(len(graph.get("edges", [])) >= expected_count - 1, "Graph is not connected by at least the temporal chain")

    node_ids = {node["id"] for node in graph["nodes"]}
    for edge in graph["edges"]:
        require(edge["a"] in node_ids and edge["b"] in node_ids, f"Broken graph edge: {edge}")

    space = bootstrap.get("space", {}).get("space_data", {})
    boot_nodes = space.get("nodes", [])
    require(len(boot_nodes) == expected_count, f"SPHR bootstrap does not contain {expected_count} panorama nodes")
    require(space.get("initialNode") in node_ids, "SPHR initialNode is not a graph node")
    invalid_pano_urls = [
        node.get("image", "")
        for node in boot_nodes
        if node.get("image")
        and not (
            str(node["image"]).startswith("/datasets/")
            or str(node["image"]).startswith("/demo/")
            or str(node["image"]).startswith("http://")
            or str(node["image"]).startswith("https://")
        )
    ]
    require(
        not invalid_pano_urls,
        "SPHR would interpret these panorama image paths as IIIF identifiers instead of direct files: "
        f"{invalid_pano_urls}",
    )
    assets = package / "assets" / "panos"
    missing_assets = [node["uuid"] for node in boot_nodes if not (assets / f"{node['uuid']}.webp").is_file()]
    require(not missing_assets, f"Missing panorama assets: {missing_assets}")

    geometry_summary = root / "02_geometry" / "summary.json"
    geometry = "PanoVGGT" if geometry_summary.is_file() else "fallback graph (GPU geometry not run/succeeded)"
    report = {
        "scene": args.scene,
        "valid": True,
        "rgb_only": True,
        "dataset_rgb_panoramas": len(rgb),
        "graph_nodes": len(graph["nodes"]),
        "graph_edges": len(graph["edges"]),
        "geometry": geometry,
        "viewer_assets": len(list(assets.glob("*.webp"))),
    }
    (package / "validation.json").write_text(json.dumps(report, indent=2))
    print("=== NAMMA PANOGRAPH: VALIDATION PASSED ===")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
