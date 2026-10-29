"""Tiny CPU-only geometry/format checks; no model imports or reconstruction."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np

MODULE = Path(__file__).resolve().parents[1] / "scripts/mvs.py"
SPEC = importlib.util.spec_from_file_location("precision_mvs", MODULE)
mvs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mvs
SPEC.loader.exec_module(mvs)


def reconstruction(*, disconnected=False, stationary=False, error=0.5, orphan=False):
    count = 6 if disconnected else 3
    images = {i: NS(name=f"{i:06d}.png", projection_center=lambda i=i: np.array(
        [0.0 if stationary else i * 0.5, 0.0, 0.0])) for i in range(count)}
    points = {}
    for p in range(12 if disconnected else 6):
        ids = range(3, 6) if disconnected and p >= 6 else range(3)
        points[p] = NS(xyz=np.array([p * 0.03, 0.2, 4.0]), error=error,
                       track=NS(elements=[NS(image_id=i) for i in ids]))
    if orphan:
        images[count] = NS(name="orphan.png", projection_center=lambda: np.array([2, 0, 0]))
    return NS(images=images, points3D=points)


class QualityTests(unittest.TestCase):
    def qa(self, model):
        return mvs.quality_report(model, min_points=5, min_observations=3, min_shared_points=3)

    def test_good_geometry_passes_and_neighbors_are_bounded(self):
        report = self.qa(reconstruction())
        self.assertTrue(report["passed"], report["failures"])
        self.assertEqual(len(report["source_neighbors"]), 3)
        self.assertTrue(all(len(v) == 2 for v in report["source_neighbors"].values()))

    def test_registered_without_tracks_fails(self):
        model = reconstruction()
        model.points3D = {}
        self.assertFalse(self.qa(model)["passed"])

    def test_stationary_capture_fails_parallax(self):
        report = self.qa(reconstruction(stationary=True))
        self.assertFalse(report["passed"])
        self.assertTrue(any("triangulation" in v for v in report["failures"]))

    def test_disconnected_components_fail(self):
        report = self.qa(reconstruction(disconnected=True))
        self.assertFalse(report["passed"])
        self.assertEqual(report["connected_views"], 3)

    def test_bad_reprojection_and_orphan_camera_fail(self):
        self.assertFalse(self.qa(reconstruction(error=8))["passed"])
        report = self.qa(reconstruction(orphan=True))
        self.assertTrue(any("orphan" in v for v in report["failures"]))

    def test_nonfinite_sparse_point_fails(self):
        model = reconstruction()
        model.points3D[0].xyz[0] = np.nan
        self.assertFalse(self.qa(model)["passed"])


class OutputPathTests(unittest.TestCase):
    def test_precision_scene_subdirectory_is_allowed(self):
        mvs.validate_output_location(Path("/outputs/precision/scene"),
                                     Path("/outputs/precision/scene/04_mvs"))

    def test_source_stage_and_ancestor_are_protected(self):
        scene = Path("/outputs/precision/scene")
        for output in (scene, scene.parent, scene / "01_frames/nested", scene / "03_sfm/model/test"):
            with self.assertRaises(ValueError):
                mvs.validate_output_location(scene, output)


class DenseReaderTests(unittest.TestCase):
    def write(self, directory, array):
        path = Path(directory) / "array.bin"
        h, w = array.shape[:2]
        channels = 1 if array.ndim == 2 else array.shape[2]
        tensor = array[:, :, None] if channels == 1 else array
        payload = tensor.transpose(1, 0, 2).reshape(-1, order="F").astype("<f4")
        path.write_bytes(f"{w}&{h}&{channels}&".encode() + payload.tobytes())
        return path

    def test_depth_and_normal_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            for shape in ((2, 3), (1, 3), (3, 1), (2, 3, 3)):
                expected = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
                actual = mvs.read_colmap_array(self.write(directory, expected))
                np.testing.assert_array_equal(actual, expected)
                self.assertFalse(actual.flags.writeable)

    def test_truncation_trailing_data_and_bad_header_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            for content in (b"2&3&1&", b"-2&3&1&", b"1&1&1&12345", b"1&1&1", b"999999&1&1&"):
                path = Path(directory) / "bad.bin"
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    mvs.read_colmap_array(path)


if __name__ == "__main__":
    unittest.main()
