import unittest

import numpy as np

from raw_viewer.app import frame_bytes, unpack_raw8, unpack_raw10, unpack_raw12


def pack_raw10(values):
    pixels = values.reshape(-1, 4).astype(np.uint16)
    packed = np.empty((pixels.shape[0], 5), dtype=np.uint8)
    packed[:, :4] = (pixels >> 2).astype(np.uint8)
    packed[:, 4] = (pixels[:, 0] & 3) | ((pixels[:, 1] & 3) << 2) | ((pixels[:, 2] & 3) << 4) | ((pixels[:, 3] & 3) << 6)
    return packed.reshape(-1)


def pack_raw12(values):
    pixels = values.reshape(-1, 2).astype(np.uint16)
    packed = np.empty((pixels.shape[0], 3), dtype=np.uint8)
    packed[:, 0:2] = (pixels >> 4).astype(np.uint8)
    packed[:, 2] = ((pixels[:, 0] & 15) | ((pixels[:, 1] & 15) << 4)).astype(np.uint8)
    return packed.reshape(-1)


class RawFormatTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(20260720)
        self.width, self.height = 12, 8

    def test_frame_sizes(self):
        self.assertEqual(frame_bytes(self.width, self.height, "RAW8"), 96)
        self.assertEqual(frame_bytes(self.width, self.height, "RAW10"), 120)
        self.assertEqual(frame_bytes(self.width, self.height, "RAW12"), 144)

    def test_raw8_round_trip(self):
        source = self.rng.integers(0, 256, (self.height, self.width), dtype=np.uint8)
        np.testing.assert_array_equal(unpack_raw8(source.reshape(-1), self.width, self.height), source)

    def test_raw10_round_trip(self):
        source = self.rng.integers(0, 1024, (self.height, self.width), dtype=np.uint16)
        np.testing.assert_array_equal(unpack_raw10(pack_raw10(source), self.width, self.height), source)

    def test_raw12_round_trip(self):
        source = self.rng.integers(0, 4096, (self.height, self.width), dtype=np.uint16)
        np.testing.assert_array_equal(unpack_raw12(pack_raw12(source), self.width, self.height), source)


if __name__ == "__main__":
    unittest.main()
