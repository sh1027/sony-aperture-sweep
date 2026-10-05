from dataclasses import replace
import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sonycam.backend import MockCamera
from sonycam.capture import run_sequence
from sonycam.config import Config
from sonycam.storage import write_metadata


class MetadataStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "metadata.json"
        self.data = {"status": "complete", "captures": [{"filename": "f2p8.jpg", "aperture": 2.8}]}

    def test_final_metadata_is_created_without_replacement(self):
        with patch("sonycam.storage.os.replace", side_effect=AssertionError("Must not replace metadata")):
            write_metadata(self.root, self.data)
        self.assertEqual(json.loads(self.target.read_text()), self.data)
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_existing_metadata_is_never_overwritten(self):
        self.target.write_bytes(b"existing metadata")
        with self.assertRaises(FileExistsError):
            write_metadata(self.root, self.data)
        self.assertEqual(self.target.read_bytes(), b"existing metadata")

    def test_invalid_json_does_not_create_a_partial_file(self):
        with self.assertRaises(ValueError):
            write_metadata(self.root, {"distance": float("nan")})
        self.assertFalse(self.target.exists())

    def test_metadata_is_saved_once_after_capture_and_cleanup(self):
        for outcome in ("complete", "capture_error", "interrupted", "restore_error", "close_error"):
            with self.subTest(outcome=outcome):
                scene = self.root / outcome
                target = scene / "metadata.json"
                test = self
                class ObservedCamera(MockCamera):
                    closed = False
                    exposures = 0
                    def capture(self, directory):
                        test.assertFalse(target.exists())
                        self.exposures += 1
                        if self.exposures == 2:
                            if outcome == "capture_error":
                                raise TimeoutError("Transfer failed")
                            if outcome == "interrupted":
                                raise KeyboardInterrupt()
                        return super().capture(directory)
                    def restore(self):
                        test.assertFalse(target.exists())
                        super().restore()
                        if outcome == "restore_error":
                            raise OSError("Restore failed")
                        return []
                    def close(self):
                        test.assertFalse(target.exists())
                        self.closed = True
                        if outcome == "close_error":
                            raise OSError("Disconnect failed")
                config = replace(Config(), output_dir=self.root, apertures=(2.8, 4.0), settle_seconds=0)
                camera = ObservedCamera(config)
                def save_after_cleanup(directory, data):
                    self.assertTrue(camera.restored)
                    self.assertTrue(camera.closed)
                    write_metadata(directory, data)
                with patch("sonycam.capture.write_metadata", side_effect=save_after_cleanup) as save:
                    if outcome in ("capture_error", "interrupted"):
                        exception = TimeoutError if outcome == "capture_error" else KeyboardInterrupt
                        with self.assertRaises(exception):
                            run_sequence(config, camera, outcome, simulated=True, progress=lambda _: None)
                    else:
                        run_sequence(config, camera, outcome, simulated=True, progress=lambda _: None)
                    save.assert_called_once()
                data = json.loads(target.read_text())
                expected_status = {"complete": "complete", "capture_error": "failed",
                                   "interrupted": "interrupted", "restore_error": "complete_with_cleanup_errors",
                                   "close_error": "complete_with_cleanup_errors"}[outcome]
                self.assertEqual(data["status"], expected_status)
                self.assertTrue((scene / "f2p8.jpg").exists())
                self.assertEqual(camera.exposures, 2)
                if outcome in ("capture_error", "interrupted"):
                    self.assertEqual([row["status"] for row in data["captures"]], ["complete", expected_status])
                    self.assertEqual(data["captures"][1], {"filename": "f4p0.jpg", "status": expected_status})
                    self.assertFalse((scene / "f4p0.jpg").exists())
                if outcome in ("restore_error", "close_error"):
                    self.assertEqual(len(data["cleanup_errors"]), 1)

    def test_final_save_failure_keeps_images_and_camera_cleanup(self):
        class CountedCamera(MockCamera):
            exposures = 0
            closed = False
            def capture(self, directory):
                self.exposures += 1
                return super().capture(directory)
            def close(self):
                self.closed = True
        config = replace(Config(), output_dir=self.root, apertures=(2.8, 4.0), settle_seconds=0)
        camera = CountedCamera(config)
        with patch("sonycam.capture.write_metadata", side_effect=PermissionError(errno.EACCES, "Access denied")) as save:
            with self.assertRaises(PermissionError):
                run_sequence(config, camera, "save-failed", simulated=True, progress=lambda _: None)
            save.assert_called_once()
        self.assertEqual(camera.exposures, 2)
        self.assertTrue(camera.restored)
        self.assertTrue(camera.closed)
        scene = self.root / "save-failed"
        self.assertEqual(len(list(scene.glob("*.jpg"))), 2)
        self.assertFalse((scene / "metadata.json").exists())


if __name__ == "__main__":
    unittest.main()
