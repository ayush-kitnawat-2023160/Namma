"""CPU-only regression checks; no model, CUDA, NumPy, or COLMAP imports."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sfm_precision


def prepared_camera(source="/workspace/outputs/precision/scene/00_calibration/camera.yaml"):
    return {
        "known_calibration": True,
        "source_profile": source,
        "width": 1280, "height": 720,
        "fx": 920.0, "fy": 921.0, "cx": 640.0, "cy": 360.0,
        "distortion_model": "pinhole_rectified",
    }


class FakeArchive(dict):
    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


class SfMPrecisionTests(unittest.TestCase):
    def test_automatic_calibration_is_initialization_not_measurement(self):
        policy = sfm_precision.calibration_policy(prepared_camera())
        self.assertFalse(policy.fixed)
        self.assertEqual(policy.camera["fx"], 920)
        self.assertEqual(policy.source, "geocalib_rectified_initialization")

    def test_supplied_measured_profile_is_fixed(self):
        policy = sfm_precision.calibration_policy(prepared_camera("/opt/namma/config/cameras/advio15.yaml"))
        self.assertTrue(policy.fixed)
        self.assertEqual(policy.source, "supplied_profile_rectified")

    def test_renamed_automatic_profile_is_detected_from_contents(self):
        camera = prepared_camera("/tmp/renamed.yaml")
        policy = sfm_precision.calibration_policy(camera, {"source": "GeoCalib shared-intrinsic RGB-video estimate"})
        self.assertFalse(policy.fixed)

    def test_unknown_camera_uses_vggt(self):
        policy = sfm_precision.calibration_policy({"known_calibration": False})
        self.assertIsNone(policy.camera)
        self.assertFalse(policy.fixed)

    def test_missing_provenance_does_not_freeze_estimates(self):
        policy = sfm_precision.calibration_policy(prepared_camera(""))
        self.assertFalse(policy.fixed)
        self.assertIsNotNone(policy.camera)

    def test_invalid_camera_is_rejected(self):
        camera = prepared_camera()
        camera["fx"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            sfm_precision.calibration_policy(camera)
        camera["fx"] = 920
        camera["distortion_model"] = "radtan"
        with self.assertRaisesRegex(ValueError, "pinhole_rectified"):
            sfm_precision.calibration_policy(camera)

    def backend(self, output: Path, observed: dict):
        backend = SimpleNamespace()
        backend.read_camera = lambda _path: {"baseline": True}
        backend.extract_features = SimpleNamespace(confs={
            "superpoint_max": {"model": {"max_keypoints": 4096}, "preprocessing": {"resize_max": 1024}}
        })
        backend.np = SimpleNamespace(load=lambda _path: FakeArchive({
            "original_width": SimpleNamespace(item=lambda: 1280),
            "original_height": SimpleNamespace(item=lambda: 720),
        }))

        def reference(_file, _names, camera, destination):
            observed["reference_camera"] = camera
            destination.mkdir(parents=True)
            (destination / "metadata.json").write_text(json.dumps({"intrinsic_source": "known_rectified"}))
            return "reconstruction"

        def ba(_model, options):
            observed["ba_options"] = vars(options)

        backend.make_vggt_reference_model = reference
        backend.pycolmap = SimpleNamespace(bundle_adjustment=ba)

        def main():
            known = backend.read_camera(None)
            observed["mapper_refine_focal"] = not bool(known)
            backend.make_vggt_reference_model(Path("cameras.npz"), ["a.png"], known, output / "vggt_reference")
            options = SimpleNamespace(refine_focal_length=False, refine_principal_point=True, refine_extra_params=True)
            model = SimpleNamespace(update_point_3d_errors=lambda: observed.update(errors_recomputed=True))
            backend.pycolmap.bundle_adjustment(model, options)
            observed["keypoints"] = backend.extract_features.confs["superpoint_max"]["model"]["max_keypoints"]
            (output / "summary.json").write_text(json.dumps({"known_intrinsics": bool(known)}))

        backend.main = main
        return backend

    def test_adapter_preserves_estimated_k_and_refines_only_focal(self):
        with tempfile.TemporaryDirectory() as directory:
            scene = Path(directory)
            output = scene / "03_sfm"
            observed = {}
            backend = self.backend(output, observed)
            original = dict(vars(backend))
            args = SimpleNamespace(scene_root=scene, output=output, temporal_window=8,
                                   visual_neighbors=12, max_keypoints=8192, feature_max_size=1600)
            policy = sfm_precision.calibration_policy(prepared_camera())
            sfm_precision.run_backend(backend, args, policy)
            self.assertEqual(observed["reference_camera"], policy.camera)
            self.assertTrue(observed["mapper_refine_focal"])
            self.assertEqual(observed["ba_options"], {
                "refine_focal_length": True, "refine_principal_point": False,
                "refine_extra_params": False,
            })
            self.assertEqual(observed["keypoints"], 8192)
            self.assertTrue(observed["errors_recomputed"])
            summary = json.loads((output / "summary.json").read_text())
            self.assertTrue(summary["calibration"]["focal_refinement"])
            self.assertIn("not an independent", summary["validation_note"])
            self.assertEqual(vars(backend), original)
            self.assertEqual(backend.extract_features.confs["superpoint_max"]["model"]["max_keypoints"], 4096)

    def test_measured_profile_remains_fixed_in_mapper_and_ba(self):
        with tempfile.TemporaryDirectory() as directory:
            scene = Path(directory)
            output = scene / "03_sfm"
            observed = {}
            backend = self.backend(output, observed)
            args = SimpleNamespace(scene_root=scene, output=output, temporal_window=8,
                                   visual_neighbors=12, max_keypoints=8192, feature_max_size=1600)
            policy = sfm_precision.calibration_policy(prepared_camera("/opt/namma/config/cameras/measured.yaml"))
            sfm_precision.run_backend(backend, args, policy)
            self.assertFalse(observed["mapper_refine_focal"])
            self.assertFalse(observed["ba_options"]["refine_focal_length"])
            self.assertEqual(observed["reference_camera"], policy.camera)

    def test_view_limit_fails_before_loading_any_native_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            scene = Path(directory)
            images = scene / "01_frames" / "images"
            images.mkdir(parents=True)
            for index in range(161):
                (images / f"{index:06d}.png").touch()
            script = Path(sfm_precision.__file__)
            result = subprocess.run([sys.executable, str(script), "--scene-root", str(scene)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("3..160 selected images", result.stderr)
            self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
