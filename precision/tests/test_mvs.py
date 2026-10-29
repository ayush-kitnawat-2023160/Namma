"""Tiny CPU-only geometry/format checks; no model imports or reconstruction."""
import importlib.util
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

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


def with_weak_views(weak=1):
    model = reconstruction()
    model.images = {i: NS(name=f'{i:06d}.png', image_id=i,
        projection_center=lambda i=i: np.array([i * .5, 0., 0.])) for i in range(9 + weak)}
    for point in model.points3D.values():
        point.track.elements = [NS(image_id=i) for i in range(9)]
    for i in range(9, 9 + weak):
        model.points3D[100 + i] = NS(xyz=np.array([.1, .2, 4.]), error=.5,
            track=NS(elements=[NS(image_id=j) for j in (0, 1, i)]))
    return model


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

    def test_pose_bridge_does_not_need_stereo_parallax(self):
        model = reconstruction(disconnected=True)
        for image_id in range(3, 6):
            model.images[image_id].projection_center = lambda image_id=image_id: np.array([(image_id - 1) * .5, 0., 0.])
        # Cameras 2 and 3 share a center (a turn in place); their tracks
        # connect camera poses, while each has two stereo sources in its group.
        for i in range(6):
            model.points3D[100 + i] = NS(xyz=np.array([.1, .2, 4.]), error=.5,
                track=NS(elements=[NS(image_id=j) for j in (2, 3)]))
        report = self.qa(model)
        self.assertTrue(report['passed'], report['failures'])
        self.assertEqual(report['pose_component_sizes'], [6])
        self.assertEqual(report['stereo_component_sizes'], [3, 3])

    def test_explicit_pruning_keeps_supported_views_and_records_every_exclusion(self):
        model = with_weak_views()
        self.assertFalse(self.qa(model)['passed'])
        report = mvs.quality_report(model, min_points=5, min_observations=3,
            min_shared_points=3, allow_view_pruning=True)
        self.assertTrue(report['passed'], report['failures'])
        self.assertEqual(report['retained_images'], 9)
        self.assertEqual(report['excluded_images'][0]['name'], '000009.png')
        self.assertTrue(report['excluded_images'][0]['exclusion_reason'])
        self.assertNotIn('000009.png', report['source_neighbors'])
        self.assertTrue(all('000009.png' not in neighbors for neighbors in report['source_neighbors'].values()))

    def test_pruning_refuses_loss_of_more_than_twenty_percent(self):
        report = mvs.quality_report(with_weak_views(3), min_points=5, min_observations=3,
            min_shared_points=3, allow_view_pruning=True)
        self.assertFalse(report['passed'])
        self.assertEqual(report['retained_images'], 9)
        self.assertTrue(any('75.0%' in failure for failure in report['failures']))

    def test_all_reliable_tracks_contribute_angle_evidence(self):
        model = reconstruction()
        template = model.points3D[0]
        model.points3D = {i: template for i in range(5001)}
        report = self.qa(model)
        self.assertTrue(report['passed'], report['failures'])
        self.assertEqual(report['parallax_sample_tracks'], 5001)

    def test_pruning_removes_sources_that_lose_their_own_support(self):
        graph = np.zeros((5, 5), dtype=bool)
        for a, b in ((0, 1), (1, 2), (0, 2), (2, 3), (3, 4)):
            graph[a, b] = graph[b, a] = True
        self.assertEqual(mvs.supported_core(graph, set(range(5))), {0, 1, 2})


class AuditTests(unittest.TestCase):
    def test_audit_reports_failed_quality_without_running_stereo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '01_frames/images').mkdir(parents=True)
            (root / '03_sfm/model').mkdir(parents=True)
            output = root / '09_audit/test'
            model = with_weak_views()
            model.update_point_3d_errors = mock.Mock()
            args = ['mvs', '--source', str(root), '--output', str(output), '--audit-only',
                    '--min-points', '5', '--min-observations', '3', '--min-shared-points', '3']
            with mock.patch.dict(sys.modules, {'pycolmap': NS(Reconstruction=lambda path: model)}), \
                 mock.patch.object(sys, 'argv', args), mock.patch.object(mvs, 'run_command') as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                mvs.main()
            run.assert_not_called()
            model.update_point_3d_errors.assert_called_once()
            status = json.loads((output / 'summary.json').read_text())
            self.assertEqual(status['status'], 'audit_complete')
            self.assertFalse(status['quality_passed'])
            self.assertTrue((output / 'quality_report.json').is_file())

    def test_recovery_exports_only_retained_cameras_and_preserves_input(self):
        exported = []

        class FakeModel:
            def __init__(self, other):
                self.images = copy.deepcopy(other.images)
                self.points3D = copy.deepcopy(other.points3D)
                self.registered = set(self.images)

            def update_point_3d_errors(self):
                pass

            def is_image_registered(self, image_id):
                return image_id in self.registered

            def deregister_image(self, image_id):
                self.registered.remove(image_id)
                for key, point in list(self.points3D.items()):
                    point.track.elements = [entry for entry in point.track.elements if entry.image_id != image_id]
                    if len(point.track.elements) < 2:
                        del self.points3D[key]

            def write_text(self, path):
                exported.extend(sorted(self.registered))
                (Path(path) / 'images.txt').write_text('\n'.join(self.images[i].name for i in sorted(self.registered)))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '01_frames/images').mkdir(parents=True)
            (root / '03_sfm/model').mkdir(parents=True)
            model = FakeModel(with_weak_views())
            for image in model.images.values():
                (root / '01_frames/images' / image.name).touch()
            output = root / '04_mvs'

            def fake_run(output, stage, argv):
                dense = output / 'dense'
                if stage == 'undistort':
                    (dense / 'stereo/depth_maps').mkdir(parents=True)
                if stage == 'patch_match':
                    for image_id in range(9):
                        path = dense / 'stereo/depth_maps' / f'{image_id:06d}.png.geometric.bin'
                        path.write_bytes(b'1&1&1&' + np.array([4.], dtype='<f4').tobytes())
                if stage == 'stereo_fusion':
                    (dense / 'fused.ply').write_bytes(b'ply\nformat binary_little_endian 1.0\nelement vertex 1000\nend_header\n')

            factory = lambda value: FakeModel(value) if isinstance(value, FakeModel) else model
            args = ['mvs', '--source', str(root), '--output', str(output), '--allow-view-pruning',
                    '--min-points', '5', '--min-observations', '3', '--min-shared-points', '3']
            with mock.patch.dict(sys.modules, {'pycolmap': NS(Reconstruction=factory)}), \
                 mock.patch.object(sys, 'argv', args), mock.patch.object(mvs, 'run_command', side_effect=fake_run) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                mvs.main()
            self.assertEqual(run.call_count, 3)
            self.assertEqual(exported, list(range(9)))
            self.assertEqual(model.registered, set(range(10)))
            self.assertEqual(len(model.points3D[109].track.elements), 3)
            report = json.loads((output / 'quality_report.json').read_text())
            self.assertEqual(report['retained_images'], 9)
            self.assertTrue(json.loads((output / 'retained_quality_report.json').read_text())['passed'])
            self.assertNotIn('000009.png', (output / 'dense/stereo/patch-match.cfg').read_text())
            self.assertEqual(json.loads((output / 'summary.json').read_text())['status'], 'complete')


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
