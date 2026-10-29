"""Tiny arrays validate depth/camera math; no models, geometry, or GPU loads."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import cv2
import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# These pure helpers do not read COLMAP files. Keep their MVS import inert so
# this test remains independent of pycolmap and the CLI installation.
with mock.patch.dict(sys.modules, {'mvs': types.SimpleNamespace(read_colmap_array=None)}):
    dataset = load_module('build_dataset')
safe_train = load_module('safe_train')


class DepthEncodingTests(unittest.TestCase):
    def test_invalid_depth_stays_missing(self):
        depth = np.array([[0, -1, np.nan, np.inf, 1e-8]], dtype=np.float32)
        encoded = dataset.encode_inverse_depth(depth, 1000)
        self.assertEqual(encoded.dtype, np.uint16)
        np.testing.assert_array_equal(encoded, np.zeros_like(encoded))

    def test_graphdeco_normalization_recovers_inverse_depth(self):
        depth = np.array([[.5, 1, 2, 3.3, 8]], dtype=np.float32)
        scale = 65500 / np.max(1 / depth)
        encoded = dataset.encode_inverse_depth(depth, scale)
        decoded = encoded.astype(np.float32) / 65536 * (65536 / scale)
        np.testing.assert_allclose(decoded, 1 / depth, rtol=0, atol=.51 / scale)

    def test_encoding_clips_instead_of_uint16_wraparound(self):
        encoded = dataset.encode_inverse_depth(np.array([[.001]], dtype=np.float32), 1000)
        self.assertEqual(int(encoded[0, 0]), 65535)


class RecenterTests(unittest.TestCase):
    def test_centered_camera_does_not_move_pixels(self):
        array = np.arange(40, dtype=np.uint16).reshape(5, 8)
        np.testing.assert_array_equal(dataset.recenter(array, 4, 2.5, cv2.INTER_NEAREST), array)

    def test_rgb_and_depth_shift_together_with_empty_border(self):
        depth = np.zeros((5, 8), dtype=np.uint16)
        depth[2, 3] = 1000
        rgb = np.repeat((depth > 0).astype(np.uint8)[..., None], 3, axis=-1) * 255
        shifted_depth = dataset.recenter(depth, 2, 1.5, cv2.INTER_NEAREST)
        shifted_rgb = dataset.recenter(rgb, 2, 1.5, cv2.INTER_LINEAR)
        self.assertEqual(int(shifted_depth[3, 5]), 1000)
        np.testing.assert_array_equal(shifted_rgb[3, 5], [255, 255, 255])
        self.assertFalse(shifted_depth[:, :2].any())
        alpha = dataset.recenter(np.full((5, 8), 255, dtype=np.uint8), 2, 1.5, cv2.INTER_NEAREST)
        self.assertFalse(alpha[:, :2].any())
        self.assertFalse(alpha[0].any())


class GrowthBudgetTests(unittest.TestCase):
    def test_clone_and_split_transient_peaks_fit_budget(self):
        for current, maximum in ((100, 500), (100, 501), (499, 500), (500, 500)):
            slots = safe_train.growth_slots(current, maximum)
            for clones in range(slots + 1):
                splits = slots - clones
                self.assertLessEqual(current + clones + 2 * splits, maximum)
                self.assertLessEqual(current + clones + splits, maximum)

    def test_full_or_overfull_budget_cannot_grow(self):
        self.assertEqual(safe_train.growth_slots(100, 100), 0)
        self.assertEqual(safe_train.growth_slots(101, 100), 0)


if __name__ == '__main__':
    unittest.main()
