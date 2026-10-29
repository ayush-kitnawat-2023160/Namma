"""Exercise the dataset handoff with tiny RGB-D and inert model/PLY bindings."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import cv2
import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location('workflow_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_depth(path):
    # The test fixture uses a one-channel, constant, tiny stereo map.
    if not path.is_file():
        raise FileNotFoundError(path)
    return np.full((12, 16), 4, dtype=np.float32)


with mock.patch.dict(sys.modules, {'mvs': NS(read_colmap_array=read_depth)}):
    dataset = load('build_dataset')
train = load('train')


class DatasetHandoffTests(unittest.TestCase):
    def test_units_centering_and_sparse_tracks_share_the_same_transform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dense = root / '04_mvs/dense'
            (dense / 'images').mkdir(parents=True)
            (dense / 'stereo/depth_maps').mkdir(parents=True)
            camera = NS(model='PINHOLE', width=16, height=12,
                        params=np.array([10., 10., 6., 5.]))
            camera.calibration_matrix = lambda: np.array([[10., 0., 6.], [0., 10., 5.], [0., 0., 1.]])
            images = {}
            for i in range(3):
                name = f'{i:06d}.png'
                rgb = np.full((12, 16, 3), 127, np.uint8)
                cv2.imwrite(str(dense / 'images' / name), rgb)
                (dense / 'stereo/depth_maps' / (name + '.geometric.bin')).touch()
                images[i] = NS(name=name, camera_id=1, points2D=[NS(xy=np.array([3., 4.]))])
            model = NS(cameras={1: camera}, images=images)
            model.write_text = lambda path: (Path(path) / 'cameras.txt').write_text('mock model\n')
            fields = [(n, 'f4') for n in ['x', 'y', 'z', 'nx', 'ny', 'nz']]
            fields += [(n, 'u1') for n in ['red', 'green', 'blue']]
            source = np.zeros(1500, dtype=fields)
            source['z'] = 4
            source['red'] = 127
            written = []

            class FakePly:
                @staticmethod
                def read(path, mmap):
                    return {'vertex': NS(data=source)}

                def __init__(self, elements, text):
                    written.extend(elements)

                def write(self, path):
                    Path(path).write_text('mock fused PLY\n')

            fake_colmap = NS(Reconstruction=lambda path: model, CameraModelId=NS(PINHOLE='PINHOLE'))
            fake_ply = NS(PlyData=FakePly, PlyElement=NS(describe=lambda data, name: data))
            output = root / '05_gs_dataset'
            with mock.patch.dict(sys.modules, {'pycolmap': fake_colmap, 'plyfile': fake_ply}), \
                 mock.patch.object(sys, 'argv', ['build_dataset', '--mvs', str(root / '04_mvs'), '--output', str(output)]), \
                 mock.patch('builtins.print'):
                dataset.main()
            np.testing.assert_array_equal(camera.params, [10, 10, 8, 6])
            for image in images.values():
                np.testing.assert_array_equal(image.points2D[0].xy, [5, 5])
            params = json.loads((output / 'sparse/0/depth_params.json').read_text())
            encoded = cv2.imread(str(output / 'depths/000000.png'), cv2.IMREAD_UNCHANGED)
            scale = params['000000']['scale']
            self.assertAlmostEqual(float(encoded[5, 5]) / 65536 * scale, .25, places=5)
            self.assertTrue((encoded[:, :2] == 0).all())
            self.assertTrue((encoded[0] == 0).all())
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['initial_gaussians'], 1500)
            self.assertEqual(len(written[0]), 1500)
            self.assertTrue(summary['cameras_recentered'])


class TrainingHandoffTests(unittest.TestCase):
    def test_heldout_training_render_and_streaming_eval_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '05_gs_dataset').mkdir()
            (root / '05_gs_dataset/summary.json').write_text(json.dumps({'initial_gaussians': 10000}))
            with mock.patch.object(sys, 'argv', ['train', '--scene-root', str(root)]), \
                 mock.patch.dict('os.environ', {'NG_GS_ITERS': '7000'}, clear=True), \
                 mock.patch.object(train.subprocess, 'run') as run, mock.patch('builtins.print'):
                train.main()
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 3)
            self.assertIn('--eval', commands[0])
            self.assertNotIn('--train_test_exp', commands[0])
            self.assertEqual(commands[0][commands[0].index('--data_device') + 1], 'cpu')
            self.assertIn('--skip_train', commands[1])
            self.assertTrue(commands[2][1].endswith('/evaluate.py'))
            self.assertTrue((root / '06_gs_model/precision_training.json').is_file())


if __name__ == '__main__':
    unittest.main()
