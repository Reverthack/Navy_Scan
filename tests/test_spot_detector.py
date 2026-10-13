import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "edge"))

try:
    import numpy as np
    from spot_detector import box_blur, detect_spots  # noqa: E402
    HAS_NUMPY = True
    NUMPY_ERROR = ""
except (ImportError, OSError) as exc:  # OSError : DLL bloquée par une politique de sécurité
    HAS_NUMPY = False
    NUMPY_ERROR = str(exc)

SKIP_REASON = f"numpy indisponible (côté site uniquement) : {NUMPY_ERROR}"


def synthetic(seed=0, size=128, blob=(40, 60), amplitude=25.0):
    rng = np.random.default_rng(seed)
    img = 100 + rng.normal(0, 2.0, (size, size))
    yy, xx = np.mgrid[0:size, 0:size]
    cy, cx = blob
    img += amplitude * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 1.5 ** 2))
    return img


@unittest.skipUnless(HAS_NUMPY, SKIP_REASON)
class BoxBlurTests(unittest.TestCase):
    def test_constant_image_is_unchanged(self):
        img = np.full((10, 12), 7.0)
        self.assertTrue(np.allclose(box_blur(img, 2), 7.0))

    def test_shape_preserved(self):
        img = np.zeros((20, 30))
        self.assertEqual(box_blur(img, 3).shape, (20, 30))


@unittest.skipUnless(HAS_NUMPY, SKIP_REASON)
class DetectSpotsTests(unittest.TestCase):
    def test_finds_blob_at_expected_position(self):
        spots = detect_spots(synthetic())
        self.assertTrue(spots, "aucune cible détectée")
        best = max(spots, key=lambda s: s["contrast"])
        self.assertLess(abs(best["x"] - 60), 2.0)
        self.assertLess(abs(best["y"] - 40), 2.0)

    def test_pure_noise_gives_no_spot(self):
        rng = np.random.default_rng(1)
        img = 100 + rng.normal(0, 2.0, (128, 128))
        self.assertEqual(detect_spots(img), [])

    def test_rejects_non_grayscale(self):
        with self.assertRaises(ValueError):
            detect_spots(np.zeros((4, 4, 3)))


if __name__ == "__main__":
    unittest.main()
