#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from common import env, load_config, scene_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Export PanoGraph into an SPHR 360-tour bootstrap package")
    parser.add_argument("scene", nargs="?", default="hindoor_unibs")
    args = parser.parse_args()

    root = scene_root(args.scene)
    graph_file = root / "03_graph" / "graph.json"
    if not graph_file.is_file():
        raise RuntimeError(f"Graph missing: {graph_file}. Run `graph` first.")
    graph = json.loads(graph_file.read_text())
    viewer_cfg = load_config()["viewer"]
    title = str(viewer_cfg.get("title", "Namma PanoGraph"))

    out = root / "04_sphr"
    assets = out / "assets" / "panos"
    assets.mkdir(parents=True, exist_ok=True)

    nodes = []
    tourpoints = []
    for node in graph["nodes"]:
        src = Path(node["web_panorama"])
        if not src.is_file():
            raise RuntimeError(f"Viewer panorama missing: {src}")
        dest = assets / f"{node['id']}.webp"
        shutil.copy2(src, dest)
        public_path = f"/panograph/{args.scene}/panos/{dest.name}"
        x, y, z = node["position"]
        fx, fy, fz = node["floor_position"]
        nodes.append({
            "uuid": node["id"],
            "image": public_path,
            "position": {"x": x, "y": y, "z": z},
            "floorPosition": {"x": fx, "y": fy, "z": fz},
            "rotation": {"x": 0.0, "y": float(node.get("yaw", 0.0)), "z": 0.0},
        })
        tourpoints.append({
            "viewMode": "FPV",
            "targetType": "NODE",
            "nodeUUID": node["id"],
            "text": f"Station {node['index'] + 1}",
            "rotation": {"azimuth": 0.0, "polar": 0.0},
            "zoom": 0,
        })

    initial = nodes[0]["uuid"]
    bootstrap = {
        "space": {
            "id": args.scene,
            "title": title,
            "type": "spaces",
            "space_data": {
                "initialNode": initial,
                "nodes": nodes,
                "navigationTransition": {
                    "enabled": True,
                    "navigationMs": 850,
                    "meshFadeMs": 300,
                },
            },
        },
        "tour": {
            "id": f"{args.scene}-tour",
            "title": f"{title} Tour",
            "tour_data": {
                "defaultShowText": True,
                "spaces": [{
                    "id": args.scene,
                    "title": title,
                    "tourpoints": tourpoints,
                }],
            },
        },
        "ui": {
            "titlePart1": "Namma",
            "titlePart2": "PanoGraph",
            "enterButtonText": "Enter",
            "exploreButtonText": "Explore",
        },
    }
    (out / "bootstrap.json").write_text(json.dumps(bootstrap, indent=2))
    (out / "graph.json").write_text(json.dumps(graph, indent=2))
    summary = {
        "scene": args.scene,
        "complete": True,
        "runtime": "SPHR",
        "panorama_nodes": len(nodes),
        "tourpoints": len(tourpoints),
        "bootstrap": str(out / "bootstrap.json"),
        "viewer_url": f"http://127.0.0.1:3000/?config=/panograph/{args.scene}/bootstrap.json",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print("=== NAMMA PANOGRAPH: SPHR PACKAGE READY ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
