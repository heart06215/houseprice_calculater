import importlib.util
import unittest

from anjuke_slider_probe import locate_piece, next_pointer


class DragCalibrationTests(unittest.TestCase):
    def test_feedback_aligns_with_different_startup_offsets_and_scales(self):
        for offset, scale, target in ((4, 1, 124), (8, 0.93, 212), (0, 1.5, 180)):
            pointer = 0
            for _ in range(70):
                visible = max(0, pointer - offset) * scale
                pointer = next_pointer(pointer, target - visible, 280)
            self.assertLess(abs(max(0, pointer - offset) * scale - target), 1)

    def test_feedback_never_exceeds_the_actual_track(self):
        self.assertEqual(next_pointer(277, 100, 280), 280)
        self.assertEqual(next_pointer(2, -100, 280), 0)


@unittest.skipUnless(importlib.util.find_spec('cv2'), 'OpenCV is installed only in cloud diagnostics')
class SliderGeometryTests(unittest.TestCase):
    def test_transparent_padding_and_css_scaling_have_native_match(self):
        import cv2
        import numpy as np
        rng = np.random.default_rng(42)
        bg = rng.integers(0, 256, size=(140, 320, 3), dtype=np.uint8)
        fg = np.zeros((140, 60, 4), dtype=np.uint8)
        fg[70:110, 5:45, :3] = bg[70:110, 175:215]
        fg[70:110, 5:45, 3] = 255
        background = cv2.imencode('.png', bg)[1].tobytes()
        foreground = cv2.imencode('.png', fg)[1].tobytes()
        found = locate_piece(background, foreground, y_hint=0)
        self.assertLessEqual(abs(found['target_x'] - 175), 1)
        self.assertEqual(found['alpha_x'], 5)
        self.assertEqual(found['alpha_y'], 70)


if __name__ == '__main__':
    unittest.main()
