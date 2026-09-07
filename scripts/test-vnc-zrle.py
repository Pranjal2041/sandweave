import unittest
import numpy as np
from vnc_zrle import decode


class DecoderTest(unittest.TestCase):
    def pixels(self, data, w, h):
        tiles = list(decode(data, w, h))
        self.assertEqual(len(tiles), 1)
        return np.frombuffer(tiles[0][-1], dtype=np.uint8).reshape(h, w, 4)[:, :, :3].tolist()

    def test_partial_palette_rows(self):
        black, white = [0, 0, 0], [255, 255, 255]
        # Each three-pixel row occupies a whole byte, with five padding bits.
        self.assertEqual(self.pixels(bytes([2, 0, 0, 0, 255, 255, 255, 0b01000000, 0b10100000]), 3, 2),
                         [[black, white, black], [white, black, white]])

    def test_raw_solid_and_runs(self):
        color = [1, 2, 3]
        for data in (bytes([0, 1, 2, 3, 1, 2, 3]), bytes([1, 1, 2, 3]),
                     bytes([128, 1, 2, 3, 1]), bytes([130, 1, 2, 3, 4, 5, 6, 128, 1])):
            self.assertEqual(self.pixels(data, 2, 1), [[color, color]])

    def test_bad_stream(self):
        for data in (bytes([0, 1]), bytes([128, 1, 2, 3, 9]), bytes([129]), bytes([1, 1, 2, 3, 0])):
            with self.assertRaises(ValueError):
                self.pixels(data, 2, 1)


if __name__ == '__main__':
    unittest.main()
