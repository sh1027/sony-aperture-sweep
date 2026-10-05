from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from sonycam.backend import MockCamera
from sonycam.capture import run_sequence, validate_apertures
from sonycam.config import Config, aperture_filename
from sonycam.storage import copy_exclusive, reserve_scene, validate_scene_name


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = replace(Config.load(), output_dir=self.root / "captures", settle_seconds=0)

    def run_camera(self, camera=None, name=None, config=None):
        config = config or self.config
        return run_sequence(config, camera or MockCamera(config), name, simulated=True, progress=lambda _: None)

    def metadata(self, name="1"):
        return json.loads((self.config.output_dir / name / "metadata.json").read_text(encoding="utf-8"))

    def test_complete_sequence_layout_and_metadata(self):
        scene = self.run_camera(name="植物 A")
        expected = ["f2p8.jpg", "f4p0.jpg", "f5p6.jpg", "f8p0.jpg", "f11.jpg", "f16.jpg", "f22.jpg"]
        self.assertEqual({p.name for p in scene.iterdir()}, {*expected, "metadata.json"})
        data = self.metadata("植物 A")
        self.assertEqual(set(data), {"schema_version", "scene_name", "simulated", "status", "captures"})
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["status"], "complete")
        self.assertEqual([row["filename"] for row in data["captures"]], expected)
        for aperture, row in zip(self.config.apertures, data["captures"]):
            self.assertEqual(set(row), {
                "filename", "status", "aspect_ratio", "aperture", "focal_length_mm",
                "focus_position_raw", "exposure_seconds", "iso", "camera_model", "lens_model",
                "width", "height",
            })
            self.assertEqual(row["focal_length_mm"], 50)
            self.assertEqual(row["focus_position_raw"], 12345)
            self.assertEqual(row["width"], row["height"])
            self.assertEqual(row["aspect_ratio"], "1:1")
            self.assertEqual(row["aperture"], aperture)
            self.assertEqual(row["exposure_seconds"], 1 / 125)
            self.assertEqual(row["iso"], 100)

    def test_config_sorts_apertures_and_resolves_paths(self):
        path = self.root / "custom.json"
        path.write_text(json.dumps({"apertures": [22, 5.6, 2.8], "aspect_ratio": "3:2"}), encoding="utf-8")
        config = Config.load(path)
        self.assertEqual(config.apertures, (2.8, 5.6, 22))
        self.assertEqual(config.output_dir, self.root / "captures")
        config = replace(config, settle_seconds=0)
        scene = self.run_camera(config=config)
        data = json.loads((scene / "metadata.json").read_text())
        self.assertEqual([row["aperture"] for row in data["captures"]], [2.8, 5.6, 22])
        self.assertTrue(all(row["aspect_ratio"] == "3:2" for row in data["captures"]))
        with Image.open(scene / "f2p8.jpg") as image:
            self.assertEqual(image.width / image.height, 1.5)

    def test_existing_scene_never_touches_camera_or_files(self):
        scene = self.config.output_dir / "existing"
        scene.mkdir(parents=True)
        (scene / "f2p8.jpg").write_bytes(b"existing capture")
        before = {p.name: p.read_bytes() for p in scene.iterdir()}
        class ShouldNotOpen(MockCamera):
            def open(self):
                raise AssertionError("Camera must not be opened")
        with self.assertRaises(FileExistsError):
            self.run_camera(ShouldNotOpen(self.config), "existing")
        self.assertEqual(before, {p.name: p.read_bytes() for p in scene.iterdir()})

    def test_auto_number_uses_max_and_does_not_reuse_failed_scene(self):
        root = self.config.output_dir
        root.mkdir()
        (root / "1").mkdir()
        (root / "3").mkdir()
        (root / "7").write_text("also reserved")
        (root / "flowers").mkdir()
        self.assertEqual(reserve_scene(root).name, "8")
        self.assertEqual(reserve_scene(root).name, "9")

    def test_unsupported_aperture_stops_before_shutter(self):
        config = replace(self.config, apertures=(2.8, 32))
        class NoShutter(MockCamera):
            def capture(self, directory):
                raise AssertionError("Must reject before any exposure")
        camera = NoShutter(config)
        with self.assertRaisesRegex(ValueError, "does not offer"):
            self.run_camera(camera, config=config)
        self.assertEqual(self.metadata()["captures"], [])
        self.assertTrue(camera.restored)

    def test_locked_aperture_reports_ring_instead_of_unsupported_lens(self):
        class LockedRing(MockCamera):
            def state(self):
                state = super().state()
                state.update(aperture=22, supported_apertures=[])
                state["aperture_property"]["writable"] = False
                return state
            def capture(self, directory):
                raise AssertionError("Must not release shutter with a locked aperture ring")
        camera = LockedRing(self.config)
        with self.assertRaisesRegex(ValueError, "aperture ring to A"):
            self.run_camera(camera)
        self.assertEqual(self.metadata()["captures"], [])
        self.assertTrue(camera.restored)

    def test_empty_or_missing_aperture_information_is_not_lens_incompatibility(self):
        for prop, message in ((None, "Aperture information"),
                              ({"readable": True, "writable": True}, "list of available apertures")):
            with self.subTest(prop=prop), self.assertRaisesRegex(ValueError, message):
                validate_apertures({"aperture_property": prop, "supported_apertures": []}, self.config.apertures)

    def test_real_camera_full_candidate_list(self):
        state = MockCamera(self.config).state()
        state["supported_apertures"] = [2.8, 3.2, 3.5, 4, 4.5, 5, 5.6, 6.3, 7.1,
                                        8, 9, 10, 11, 13, 14, 16, 18, 20, 22]
        validate_apertures(state, self.config.apertures)

    def test_optional_measured_distance_is_omitted_or_recorded(self):
        class CameraDistance(MockCamera):
            distance_raw = None
            def state(self):
                return {**super().state(), "focus_distance_raw": self.distance_raw}
        config = replace(self.config, apertures=(2.8,))
        for raw in (None, 2000, 0xFFFFFFFF):
            with self.subTest(distance_raw=raw):
                camera = CameraDistance(config)
                camera.distance_raw = raw
                name = f"distance-{raw}"
                self.run_camera(camera, config=config, name=name)
                data = self.metadata(name)
                self.assertEqual(data["status"], "complete")
                self.assertEqual(len(data["captures"]), 1)
                self.assertFalse(any(key.startswith("focus_distance_") for key in data["captures"][0]))
        config = replace(config, focus_distance_m=1.25)
        self.run_camera(CameraDistance(config), config=config, name="measured")
        row = self.metadata("measured")["captures"][0]
        self.assertEqual(row["focus_distance_m"], 1.25)
        self.assertEqual(row["focus_distance_source"], "user_measured")
        self.assertFalse(row["focus_distance_is_infinity"])

    def test_wrong_aperture_and_wrong_ratio_preserve_transfer(self):
        for kind in ("aperture", "ratio"):
            with self.subTest(kind=kind):
                class BadImage(MockCamera):
                    def capture(self, directory):
                        if kind == "aperture":
                            self.aperture = 8
                        else:
                            self.aspect = "3:2"
                        return super().capture(directory)
                with self.assertRaises(ValueError):
                    self.run_camera(BadImage(self.config), name=kind)
                scene = self.config.output_dir / kind
                self.assertFalse((scene / "f2p8.jpg").exists())
                self.assertTrue((scene / ".incoming/001/DSC00001.JPG").exists())
                self.assertEqual(self.metadata(kind)["status"], "failed")
                if kind == "aperture":
                    # Record the received image's actual F value, even on mismatch.
                    self.assertEqual(self.metadata(kind)["captures"][0]["aperture"], 8)

    def test_focus_move_stops_sequence_before_next_exposure(self):
        class MovingFocus(MockCamera):
            def state(self):
                data = super().state()
                if self.aperture == 4:
                    data["focus_position_raw"] += 1
                return data
        with self.assertRaisesRegex(ValueError, "Focus/zoom changed"):
            self.run_camera(MovingFocus(self.config))
        self.assertEqual(self.metadata()["captures"][1]["status"], "failed")
        self.assertNotIn("focus_position_raw", self.metadata()["captures"][1])

    def test_keyboard_interrupt_saves_partial_results_and_restores(self):
        class Interrupted(MockCamera):
            def capture(self, directory):
                raise KeyboardInterrupt()
        camera = Interrupted(self.config)
        with self.assertRaises(KeyboardInterrupt):
            self.run_camera(camera)
        self.assertTrue(camera.restored)
        self.assertEqual(self.metadata()["status"], "interrupted")

    def test_restore_failure_is_recorded(self):
        class BadRestore(MockCamera):
            def restore(self):
                return ["camera disconnected before restore"]
        self.run_camera(BadRestore(self.config))
        data = self.metadata()
        self.assertEqual(data["status"], "complete_with_cleanup_errors")
        self.assertEqual(len(data["cleanup_errors"]), 1)

    def test_copy_refuses_existing_destination(self):
        source, dest = self.root / "source", self.root / "destination"
        source.write_bytes(b"new")
        dest.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            copy_exclusive(source, dest)
        self.assertEqual(dest.read_bytes(), b"old")

    def test_invalid_names_and_config(self):
        for name in ("", "..", "../escape", "C:\\escape", "a/b", "a\nb", "NUL", "con.jpg", "COM1", "name.", "name "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_scene_name(name)
        cases = [{"apertures": []}, {"apertures": [4, 4.0]}, {"apertures": [True]},
                 {"apertures": [float("nan")]}, {"apertures": [float("inf")]},
                 {"apertures": [2.801]}, {"apertures": [0]}, {"apertures": ["2.8"]},
                 {"aspect_ratio": "2:1"}, {"aspect_ratio": []}, {"timeout_seconds": 0},
                 {"focus_distance_m": -1}, {"settle_seconds": -1}, {"camera_index": True},
                 {"unknown": 1}, {"output_dir": ""}]
        path = self.root / "bad.json"
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                path.write_text(json.dumps(data), encoding="utf-8")
                Config.load(path)

    def test_filename_convention(self):
        self.assertEqual(aperture_filename(4), "f4p0.jpg")
        self.assertEqual(aperture_filename(22.0), "f22.jpg")
        self.assertEqual(aperture_filename(1.25), "f1p25.jpg")


if __name__ == "__main__":
    unittest.main()

