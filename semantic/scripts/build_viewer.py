#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import re
from pathlib import Path

import yaml


SEMANTIC_ROOT = Path("/workspace/semantic")
NEXTGEN_ROOT = Path("/workspace/nextgen")
DEFAULT_CONFIG = Path("/opt/namma-semantic/config/default.yaml")


INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Namma Digital Twin Viewer</title>
  <link rel="stylesheet" href="style.css" />
  <script src="https://cdn.babylonjs.com/babylon.js"></script>
  <script src="https://cdn.babylonjs.com/loaders/babylonjs.loaders.min.js"></script>
</head>
<body>
  <canvas id="renderCanvas"></canvas>
  <div id="hud">
    <div class="title">Namma Digital Twin</div>
    <div id="status">Loading scene…</div>
    <div id="units"></div>
    <div id="coords"></div>
    <div class="controls">
      Click view: mouse look<br />
      W A S D: move &nbsp; Shift: faster<br />
      R: reset &nbsp; G: gravity &nbsp; C: collision debug<br />
      M: Gaussian/mesh visual &nbsp; H: HUD
    </div>
  </div>
  <div id="crosshair">+</div>
  <div id="warning" class="hidden"></div>
  <script src="app.js"></script>
</body>
</html>
'''


STYLE_CSS = r'''html, body {
  width: 100%; height: 100%; margin: 0; overflow: hidden;
  background: #111; font-family: Inter, system-ui, sans-serif;
}
#renderCanvas { width: 100%; height: 100%; touch-action: none; display: block; outline: none; }
#hud {
  position: fixed; top: 14px; left: 14px; z-index: 10;
  min-width: 270px; padding: 12px 14px; color: #f3f3f3;
  background: rgba(10, 12, 15, 0.78); border: 1px solid rgba(255,255,255,.14);
  border-radius: 10px; backdrop-filter: blur(7px); font-size: 13px; line-height: 1.45;
  pointer-events: none;
}
#hud .title { font-weight: 700; font-size: 15px; margin-bottom: 5px; }
#hud .controls { margin-top: 8px; opacity: .76; }
#crosshair {
  position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%);
  color: rgba(255,255,255,.75); font-size: 19px; z-index: 9; pointer-events: none;
}
#warning {
  position: fixed; right: 14px; top: 14px; z-index: 12; max-width: 420px;
  padding: 11px 13px; border-radius: 9px; color: #fff3d1;
  background: rgba(100, 67, 0, .86); border: 1px solid rgba(255, 205, 90, .5);
  font-size: 13px; line-height: 1.4;
}
.hidden { display: none; }
'''


APP_JS = r'''(() => {
  const canvas = document.getElementById("renderCanvas");
  const statusEl = document.getElementById("status");
  const unitsEl = document.getElementById("units");
  const coordsEl = document.getElementById("coords");
  const warningEl = document.getElementById("warning");
  const hudEl = document.getElementById("hud");

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function quaternionFromTo(fromArray, toArray) {
    const from = BABYLON.Vector3.FromArray(fromArray).normalize();
    const to = BABYLON.Vector3.FromArray(toArray).normalize();
    const dot = clamp(BABYLON.Vector3.Dot(from, to), -1, 1);
    if (dot > 0.999999) return BABYLON.Quaternion.Identity();
    if (dot < -0.999999) {
      let axis = BABYLON.Vector3.Cross(from, BABYLON.Axis.X);
      if (axis.lengthSquared() < 1e-8) axis = BABYLON.Vector3.Cross(from, BABYLON.Axis.Z);
      axis.normalize();
      return BABYLON.Quaternion.RotationAxis(axis, Math.PI);
    }
    const axis = BABYLON.Vector3.Cross(from, to).normalize();
    return BABYLON.Quaternion.RotationAxis(axis, Math.acos(dot));
  }

  function rotateVector(vector, quaternion) {
    const m = BABYLON.Matrix.Identity();
    quaternion.toRotationMatrix(m);
    return BABYLON.Vector3.TransformCoordinates(vector, m);
  }

  function parentImported(result, root) {
    const nodes = [...(result.transformNodes || []), ...(result.meshes || [])];
    const nodeSet = new Set(nodes);
    for (const node of nodes) {
      if (node === root) continue;
      if (!node.parent || !nodeSet.has(node.parent)) node.parent = root;
    }
  }

  async function main() {
    if (!window.BABYLON) throw new Error("Babylon.js failed to load. Check internet access for the CDN dependencies.");
    const [config, graph] = await Promise.all([
      fetch("viewer_config.json").then(r => r.json()),
      fetch("scene_graph.json").then(r => r.json()),
    ]);

    const engine = new BABYLON.Engine(canvas, true, { preserveDrawingBuffer: false, stencil: true });
    const scene = new BABYLON.Scene(engine);
    scene.clearColor = new BABYLON.Color4(0.055, 0.065, 0.08, 1.0);
    scene.collisionsEnabled = true;
    scene.gravity = new BABYLON.Vector3(0, -config.gravity, 0);

    const hemi = new BABYLON.HemisphericLight("hemi", new BABYLON.Vector3(0, 1, 0), scene);
    hemi.intensity = 1.15;
    const sun = new BABYLON.DirectionalLight("sun", new BABYLON.Vector3(-0.45, -1, 0.25), scene);
    sun.intensity = 0.7;

    const worldRotation = quaternionFromTo(graph.up_vector, [0, 1, 0]);
    const meshRoot = new BABYLON.TransformNode("mesh_world", scene);
    const gaussianRoot = new BABYLON.TransformNode("gaussian_world", scene);
    const collisionRoot = new BABYLON.TransformNode("collision_world", scene);
    meshRoot.rotationQuaternion = worldRotation;
    gaussianRoot.rotationQuaternion = worldRotation;
    collisionRoot.rotationQuaternion = worldRotation;

    statusEl.textContent = "Loading explicit mesh fallback…";
    const visual = await BABYLON.SceneLoader.ImportMeshAsync(null, "assets/", "digital_twin.glb", scene);
    parentImported(visual, meshRoot);
    for (const mesh of visual.meshes) {
      mesh.checkCollisions = false;
      mesh.isPickable = false;
    }

    let gaussian = null;
    if (config.gaussian_asset) {
      statusEl.textContent = "Loading Gaussian splat visual…";
      gaussian = await BABYLON.SceneLoader.ImportMeshAsync(null, "assets/", config.gaussian_asset, scene);
      parentImported(gaussian, gaussianRoot);
      const gaussianScale = config.gaussian_scale || 1.0;
      gaussianRoot.scaling = new BABYLON.Vector3(gaussianScale, gaussianScale, gaussianScale);
      for (const mesh of gaussian.meshes) {
        mesh.checkCollisions = false;
        mesh.isPickable = false;
      }
    }

    statusEl.textContent = "Loading collision GLB…";
    const collision = await BABYLON.SceneLoader.ImportMeshAsync(null, "assets/", "collision_scene.glb", scene);
    parentImported(collision, collisionRoot);
    for (const mesh of collision.meshes) {
      mesh.checkCollisions = true;
      mesh.isPickable = false;
      mesh.visibility = 0.0;
    }

    let gaussianVisual = Boolean(gaussian) && config.visual_mode !== "mesh";
    const units = graph.units.metric_calibrated ? "metres" : "reconstruction units";
    const updateVisualMode = () => {
      meshRoot.setEnabled(!gaussianVisual);
      gaussianRoot.setEnabled(gaussianVisual && Boolean(gaussian));
      const visualLabel = gaussianVisual ? "Gaussian visual" : "explicit mesh visual";
      unitsEl.textContent = `${graph.nodes.length} semantic assets · ${units} · ${visualLabel}`;
    };
    updateVisualMode();

    const initial = config.initial_view;
    const spawnRaw = initial ? BABYLON.Vector3.FromArray(initial.position) : BABYLON.Vector3.FromArray(graph.bounds.center);
    const forwardRaw = initial ? BABYLON.Vector3.FromArray(initial.forward) : new BABYLON.Vector3(0, 0, 1);
    const spawn = rotateVector(spawnRaw, worldRotation);
    const forward = rotateVector(forwardRaw, worldRotation).normalize();

    const camera = new BABYLON.UniversalCamera("player", spawn.clone(), scene);
    camera.setTarget(spawn.add(forward));
    camera.minZ = Math.max(config.scene_span * 0.001, 0.0005);
    camera.speed = config.movement_speed;
    camera.angularSensibility = 3200;
    camera.inertia = 0.25;
    camera.checkCollisions = true;
    camera.applyGravity = true;
    camera.ellipsoid = new BABYLON.Vector3(
      config.player_radius,
      config.player_height * 0.5,
      config.player_radius
    );
    camera.ellipsoidOffset = new BABYLON.Vector3(
      0,
      -config.player_height * 0.5 + config.player_radius,
      0
    );
    camera.keysUp.push(87);
    camera.keysDown.push(83);
    camera.keysLeft.push(65);
    camera.keysRight.push(68);
    camera.attachControl(canvas, true);

    let gravityEnabled = true;
    let collisionDebug = false;
    const reset = () => {
      camera.position.copyFrom(spawn);
      camera.setTarget(spawn.add(forward));
      camera.cameraDirection.set(0, 0, 0);
      camera.cameraRotation.set(0, 0);
    };

    canvas.addEventListener("click", () => {
      if (document.pointerLockElement !== canvas && canvas.requestPointerLock) canvas.requestPointerLock();
    });
    window.addEventListener("keydown", (event) => {
      if (event.code === "KeyR") reset();
      if (event.code === "KeyG") {
        gravityEnabled = !gravityEnabled;
        camera.applyGravity = gravityEnabled;
      }
      if (event.code === "KeyC") {
        collisionDebug = !collisionDebug;
        for (const mesh of collision.meshes) mesh.visibility = collisionDebug ? 0.28 : 0.0;
      }
      if (event.code === "KeyM" && gaussian) {
        gaussianVisual = !gaussianVisual;
        updateVisualMode();
      }
      if (event.code === "KeyH") hudEl.style.display = hudEl.style.display === "none" ? "block" : "none";
      if (event.code === "ShiftLeft" || event.code === "ShiftRight") camera.speed = config.movement_speed * 2.4;
    });
    window.addEventListener("keyup", (event) => {
      if (event.code === "ShiftLeft" || event.code === "ShiftRight") camera.speed = config.movement_speed;
    });
    if (config.warning) {
      warningEl.textContent = config.warning;
      warningEl.classList.remove("hidden");
    }
    statusEl.textContent = "Ready — click the view to capture the mouse";

    scene.onBeforeRenderObservable.add(() => {
      const p = camera.position;
      const mode = gaussianVisual ? "GS" : "mesh";
      coordsEl.textContent = `x ${p.x.toFixed(3)}   y ${p.y.toFixed(3)}   z ${p.z.toFixed(3)} · ${engine.getFps().toFixed(0)} FPS · ${mode} · gravity ${gravityEnabled ? "on" : "off"}`;
    });

    engine.runRenderLoop(() => scene.render());
    window.addEventListener("resize", () => engine.resize());
  }

  main().catch((error) => {
    console.error(error);
    statusEl.textContent = `Viewer error: ${error.message}`;
    warningEl.textContent = error.stack || String(error);
    warningEl.classList.remove("hidden");
  });
})();
'''


SERVE_PY = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Namma first-person viewer")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parent)
    server = ThreadingHTTPServer((args.bind, args.port), SimpleHTTPRequestHandler)
    print(f"Namma viewer: http://{args.bind}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
'''


README_TEMPLATE = '''Namma Digital Twin — Stage 7 browser viewer

Run locally from this directory:

    python3 serve.py --port 8000

Then open:

    http://127.0.0.1:8000

Controls:
    Click        capture mouse / look around
    W A S D      move
    Shift        move faster
    R            reset to the selected BA camera pose
    G            toggle gravity
    C            toggle collision-mesh debug display
    M            toggle Gaussian vs explicit mesh visual
    H            hide/show HUD

The viewer uses Babylon.js and the glTF loader from the Babylon CDN, so the
browser needs internet access for those two JavaScript dependencies.

If this package is on a remote GPU server, either copy 07_viewer to your laptop
or tunnel the server port over SSH, e.g.:

    ssh -L 8000:127.0.0.1:8000 <server>

and run `python3 serve.py --port 8000` on the remote server.

Scale status:
{scale_status}

The viewer rotates the fitted Stage-3 floor/up direction onto browser +Y at
runtime. The underlying reconstruction and scene graph remain in the original
COLMAP global-BA frame.
'''


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> dict:
    with DEFAULT_CONFIG.open() as handle:
        return yaml.safe_load(handle) or {}


def find_gaussian_ply(scene: str) -> Path | None:
    root = NEXTGEN_ROOT / scene / "05_gs_model" / "point_cloud"
    if not root.is_dir():
        return None
    candidates = []
    for path in root.glob("iteration_*/point_cloud.ply"):
        match = re.search(r"iteration_(\d+)$", path.parent.name)
        if match and path.is_file() and path.stat().st_size > 0:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Namma Stage 7: browser first-person viewer package")
    parser.add_argument("scene")
    parser.add_argument("--overwrite", action="store_true", default=env_bool("NS_VIEWER_OVERWRITE"))
    args = parser.parse_args()

    base = SEMANTIC_ROOT / args.scene
    stage6 = base / "06_scene"
    summary_file = stage6 / "summary.json"
    graph_file = stage6 / "scene_graph.json"
    trajectory_file = stage6 / "camera_trajectory.json"
    visual_file = stage6 / "digital_twin.glb"
    collision_file = stage6 / "collision_scene.glb"
    for required in [summary_file, graph_file, trajectory_file, visual_file, collision_file]:
        if not required.is_file():
            raise RuntimeError(f"Stage 7 prerequisite missing: {required}")

    stage6_summary = json.loads(summary_file.read_text())
    if not stage6_summary.get("complete"):
        raise RuntimeError("Stage 7 requires a complete Stage-6 scene.")
    graph = json.loads(graph_file.read_text())
    trajectory = json.loads(trajectory_file.read_text())
    config = load_config()["viewer"]
    visual_mode = os.environ.get("NS_VIEWER_VISUAL_MODE", config.get("visual_mode", "gaussian")).strip().lower()
    if visual_mode not in {"gaussian", "mesh"}:
        raise RuntimeError(f"NS_VIEWER_VISUAL_MODE must be gaussian or mesh; got {visual_mode!r}")

    output = base / "07_viewer"
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Viewer output already exists: {output}. Use --overwrite to replace it.")
        shutil.rmtree(output)
    assets = output / "assets"
    assets.mkdir(parents=True)

    shutil.copy2(visual_file, assets / "digital_twin.glb")
    shutil.copy2(collision_file, assets / "collision_scene.glb")
    shutil.copy2(graph_file, output / "scene_graph.json")
    shutil.copy2(trajectory_file, output / "camera_trajectory.json")

    gaussian_source = find_gaussian_ply(args.scene)
    gaussian_asset = None
    gaussian_iteration = None
    if gaussian_source is not None:
        gaussian_asset = "gaussian_scene.ply"
        shutil.copy2(gaussian_source, assets / gaussian_asset)
        match = re.search(r"iteration_(\d+)$", gaussian_source.parent.name)
        gaussian_iteration = int(match.group(1)) if match else None
    elif visual_mode == "gaussian":
        raise RuntimeError(
            "Gaussian viewer mode requested but no trained NextGen Gaussian PLY was found under "
            f"{NEXTGEN_ROOT / args.scene / '05_gs_model/point_cloud'}"
        )

    extents = [float(value) for value in graph["bounds"]["extents"]]
    scene_span = max(max(extents), 1e-6)
    player_radius = max(
        float(config["min_player_radius"]),
        min(float(config["max_player_radius"]), scene_span * float(config["player_radius_fraction"])),
    )
    player_height = max(
        player_radius * 3.0,
        scene_span * float(config["player_height_fraction"]),
    )
    movement_speed = max(scene_span * float(config["movement_speed_fraction"]), player_radius * 0.5)
    gravity = max(scene_span * float(config["gravity_fraction"]), player_radius * 0.15)

    start_index = int(config.get("start_camera_index", 0))
    if trajectory:
        start_index = max(0, min(start_index, len(trajectory) - 1))
        initial_view = trajectory[start_index]
    else:
        initial_view = None

    warning = None
    metric = bool(graph.get("units", {}).get("metric_calibrated"))
    if metric and scene_span < 1.5:
        warning = (
            "Metric calibration is marked active, but the complete scene spans only "
            f"{scene_span:.2f} m. Verify meters_per_unit against a real measured distance; "
            "the viewer will still run with scale-relative movement."
        )

    viewer_config = {
        "scene_id": args.scene,
        "scene_span": scene_span,
        "movement_speed": movement_speed,
        "player_height": player_height,
        "player_radius": player_radius,
        "gravity": gravity,
        "initial_view": initial_view,
        "warning": warning,
        "visual_mode": visual_mode,
        "gaussian_asset": gaussian_asset,
        "gaussian_iteration": gaussian_iteration,
        # Stage-6 visuals/collisions and BA trajectory may be uniformly scaled,
        # but the trainer PLY always remains in original BA reconstruction units.
        "gaussian_scale": float(graph.get("units", {}).get("meters_per_unit") or 1.0),
        "babylon_cdn": "https://cdn.babylonjs.com/babylon.js",
        "babylon_loaders_cdn": "https://cdn.babylonjs.com/loaders/babylonjs.loaders.min.js",
    }
    (output / "viewer_config.json").write_text(json.dumps(viewer_config, indent=2))
    (output / "index.html").write_text(INDEX_HTML)
    (output / "style.css").write_text(STYLE_CSS)
    (output / "app.js").write_text(APP_JS)
    (output / "serve.py").write_text(SERVE_PY)
    os.chmod(output / "serve.py", 0o755)

    if metric:
        scale_status = (
            f"Scene graph says metric calibrated: {graph['units'].get('meters_per_unit')} m/unit.\n"
            "Use this only if that scalar came from an actual real-world measurement."
        )
    else:
        scale_status = (
            "Uncalibrated reconstruction units. Movement, gravity, and player collider\n"
            "are scaled relative to the scene bounds, not interpreted as SI metres."
        )
    (output / "README.txt").write_text(README_TEMPLATE.format(scale_status=scale_status))

    summary = {
        "scene": args.scene,
        "complete": True,
        "viewer": "Babylon.js hybrid Gaussian + collision first-person walkthrough",
        "semantic_nodes": int(stage6_summary.get("nodes", 0)),
        "visual_triangles": int(stage6_summary.get("visual_triangles", 0)),
        "collision_triangles": int(stage6_summary.get("collision_triangles", 0)),
        "metric_calibrated": metric,
        "meters_per_unit": graph.get("units", {}).get("meters_per_unit"),
        "scene_span": scene_span,
        "movement_speed": movement_speed,
        "player_height": player_height,
        "player_radius": player_radius,
        "camera_spawn": initial_view.get("image") if initial_view else None,
        "visual_mode": visual_mode,
        "gaussian_asset": gaussian_asset,
        "gaussian_iteration": gaussian_iteration,
        "warning": warning,
        "serve": "python3 serve.py --port 8000",
        "entrypoint": "index.html",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    (output / ".complete").write_text("ok\n")
    print("=== NAMMA STAGE 7: VIEWER PACKAGE READY ===")
    print(json.dumps(summary, indent=2))
    print(f"Viewer directory: {output}")


if __name__ == "__main__":
    main()
