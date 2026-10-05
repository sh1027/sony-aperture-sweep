from dataclasses import replace
import json
from pathlib import Path
import random
import tempfile
import unittest

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from sonycam.backend import MockCamera
from sonycam.capture import run_sequence
from sonycam.config import Config
from sonycam.images import inspect_jpeg, validate_image


def write_mpo(path, aperture=2.8):
    # A square main image with EXIF, plus a non-square thumbnail, like the
    # structure of the Sony file that triggered the regression.
    pixels = random.Random(0).randbytes(128 * 128 * 3)
    primary = Image.frombytes("RGB", (128, 128), pixels)
    thumbnail = Image.new("RGB", (32, 18), "blue")
    exif = Image.Exif()
    exif[0x0110] = "TEST CAMERA"
    exif[0x8769] = {0x829D: IFDRational(aperture), 0x920A: IFDRational(50),
                    0xA434: "TEST LENS"}
    primary.save(path, format="MPO", save_all=True, append_images=[thumbnail], exif=exif)
    return path.read_bytes()


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_mpo_uses_main_image_dimensions_and_exif(self):
        path = self.root / "DSC00001.JPG"
        original = write_mpo(path)
        info = inspect_jpeg(path)
        self.assertEqual(info["container_format"], "MPO")
        self.assertEqual(info["frame_count"], 2)
        self.assertEqual(info["validated_frame"], 0)
        self.assertEqual((info["width"], info["height"]), (128, 128))
        self.assertEqual(info["aperture"], 2.8)
        self.assertEqual(info["focal_length_mm"], 50)
        self.assertEqual(info["lens_model"], "TEST LENS")
        validate_image(info, 2.8, "1:1")
        self.assertEqual(path.read_bytes(), original)

    def test_mpo_capture_preserves_original_file_bytes(self):
        config = replace(Config.load(), output_dir=self.root / "captures",
                         apertures=(2.8,), settle_seconds=0)
        class MpoCamera(MockCamera):
            def capture(self, directory):
                path = directory / "DSC00001.JPG"
                self.transferred = write_mpo(path, self.aperture)
                return path
        camera = MpoCamera(config)
        scene = run_sequence(config, camera, simulated=True, progress=lambda _: None)
        self.assertEqual((scene / "f2p8.jpg").read_bytes(), camera.transferred)
        data = json.loads((scene / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "complete")
        row = data["captures"][0]
        self.assertEqual((row["width"], row["height"]), (128, 128))
        self.assertEqual(row["aperture"], 2.8)
        self.assertEqual(row["lens_model"], "TEST LENS")
        self.assertNotIn("image", row)
        self.assertNotIn("container_format", row)

    def test_truncated_mpo_main_image_is_rejected(self):
        path = self.root / "broken.JPG"
        data = write_mpo(path)
        with Image.open(path) as image:
            main_size = image.mpinfo[0xB002][0]["Size"]
        path.write_bytes(data[:main_size * 3 // 4])
        with self.assertRaises(OSError):
            inspect_jpeg(path)

    def test_non_jpeg_with_jpg_extension_is_rejected(self):
        path = self.root / "not-jpeg.jpg"
        Image.new("RGB", (100, 100)).save(path, format="PNG")
        with self.assertRaisesRegex(ValueError, "PNG"):
            inspect_jpeg(path)

    def test_ordinary_jpeg_with_nested_exif(self):
        path = self.root / "ordinary.jpg"
        exif = Image.Exif()
        exif[0x8769] = {0x829D: IFDRational(28, 10), 0x920A: IFDRational(35), 0xA434: "FE 35mm"}
        Image.new("RGB", (100, 100)).save(path, exif=exif)
        info = inspect_jpeg(path)
        self.assertEqual(info["container_format"], "JPEG")
        self.assertEqual(info["frame_count"], 1)
        self.assertEqual(info["focal_length_mm"], 35)
        self.assertEqual(info["lens_model"], "FE 35mm")
        validate_image(info, 2.8, "1:1")


if __name__ == "__main__":
    unittest.main()
