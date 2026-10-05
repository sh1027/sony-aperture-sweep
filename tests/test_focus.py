from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sonycam.backend import CameraError, MockCamera, SonyCamera
from sonycam.capture import run_sequence
from sonycam.cli import main
from sonycam.config import Config
from sonycam.focus import focus_info, run_focus_action


class FocusTests(unittest.TestCase):
    def test_info_does_not_move_or_shoot_even_with_config_target(self):
        config = replace(Config(), focus_position_raw=22000, focus_distance_m=0.5)
        class ReadOnly(MockCamera):
            def set_focus_position(self, value):
                raise AssertionError("Inspection must not move focus")
            def capture(self, directory):
                raise AssertionError("Inspection must not shoot")
            def state(self):
                return {**super().state(), "focus_distance_raw": None}
        camera = ReadOnly(config)
        with patch("sonycam.cli.Config.load", return_value=config), \
             patch("sonycam.cli.SonyCamera", return_value=camera), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--focus-info"]), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["focus_position_raw"], 12345)
        # The configured measurement is not a live camera reading.
        self.assertIsNone(data["focus_distance_m"])
        self.assertEqual(data["focus_distance_source"], "unavailable")
        self.assertFalse(camera.restored)

    def test_move_only_reads_back_position_and_restores_settings(self):
        class NoShutter(MockCamera):
            def capture(self, directory):
                raise AssertionError("Focus adjustment must not shoot")
        camera = NoShutter(Config())
        result = run_focus_action(camera, 22000)
        self.assertEqual(result["previous_focus_position_raw"], 12345)
        self.assertEqual(result["requested_focus_position_raw"], 22000)
        self.assertEqual(result["focus_position_raw"], 22000)
        self.assertTrue(camera.restored)
        self.assertEqual(camera.focus_position, 22000)

    def test_move_failure_restores_and_disconnects(self):
        class FailedMove(MockCamera):
            closed = False
            def set_focus_position(self, value):
                raise CameraError("focus did not complete")
            def close(self):
                self.closed = True
        camera = FailedMove(Config())
        with self.assertRaisesRegex(CameraError, "did not complete"):
            run_focus_action(camera, 22000)
        self.assertTrue(camera.restored)
        self.assertTrue(camera.closed)

    def test_capture_uses_new_position_as_baseline_and_records_it(self):
        class PositionOnlyCamera(MockCamera):
            def state(self):
                return {**super().state(), "focus_distance_raw": None}
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(), output_dir=Path(directory), settle_seconds=0,
                             apertures=(2.8, 4.0), focus_position_raw=22000)
            camera = PositionOnlyCamera(config)
            scene = run_sequence(config, camera, simulated=True, progress=lambda _: None)
            data = json.loads((scene / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "complete")
        self.assertEqual(len(data["captures"]), 2)
        for row in data["captures"]:
            self.assertEqual(row["focus_position_raw"], 22000)
            self.assertFalse(any(key.startswith("focus_distance_") for key in row))

    def test_unfinished_focus_stops_before_shutter(self):
        class StillMoving(MockCamera):
            def set_focus_position(self, value):
                return {**super().set_focus_position(value), "focus_driving_status_raw": 2}
            def capture(self, directory):
                raise AssertionError("Must not shoot while moving")
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(), output_dir=Path(directory), focus_position_raw=22000)
            camera = StillMoving(config)
            with self.assertRaisesRegex(ValueError, "stationary"):
                run_sequence(config, camera, simulated=True, progress=lambda _: None)
            data = json.loads((Path(directory) / "1/metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["captures"], [])
        self.assertTrue(camera.restored)

    def test_range_validation_and_bridge_command(self):
        camera = SonyCamera(Config())
        with patch.object(camera, "command", return_value={}) as command:
            for value in (0, 65535):
                camera.set_focus_position(value)
                command.assert_called_with(f"focus {value}", multiplier=3)
            command.reset_mock()
            for value in (-1, 65536, 0.5, True, "22000"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    camera.set_focus_position(value)
            command.assert_not_called()

    def test_config_rejects_invalid_raw_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (-1, 65536, 0.5, True, "22000"):
                path.write_text(json.dumps({"focus_position_raw": value}))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config.load(path)
            path.write_text(json.dumps({"focus_position_raw": 22000}))
            self.assertEqual(Config.load(path).focus_position_raw, 22000)

    def test_measured_distance_option_replaces_legacy_aliases(self):
        with patch("sonycam.cli.Config.load", return_value=Config()), \
             patch("sonycam.cli.run_sequence", return_value=Path("sample")) as run, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--mock", "--measured-focus-distance-m", "0.5"]), 0)
            config = run.call_args.args[0]
            self.assertEqual(config.focus_distance_m, 0.5)
            self.assertIsNone(config.focus_position_raw)
            run.reset_mock()
            for option in ("--focus-distance", "--focus-distance-m"):
                with self.subTest(option=option), self.assertRaises(SystemExit) as error:
                    main(["--mock", option, "0.5"])
                self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_info_distinguishes_distance_infinity_and_unavailable(self):
        for raw, metres, infinity, source in (
            (1500, 1.5, False, "camera_sdk"),
            (0xFFFFFFFF, None, True, "camera_sdk"),
            (None, None, False, "unavailable"),
            (0, None, False, "unavailable"),
        ):
            with self.subTest(raw=raw):
                info = focus_info({"focus_distance_raw": raw})
                self.assertEqual(info["focus_distance_m"], metres)
                self.assertEqual(info["focus_distance_is_infinity"], infinity)
                self.assertEqual(info["focus_distance_source"], source)
                json.dumps(info, allow_nan=False)

    def test_cleanup_failures_preserve_focus_error_and_still_disconnect(self):
        class FailedCamera(MockCamera):
            closed = False
            def set_focus_position(self, value):
                raise CameraError("focus did not complete")
            def restore(self):
                raise CameraError("restore failed")
            def close(self):
                self.closed = True
                raise CameraError("disconnect failed")
        camera = FailedCamera(Config())
        with redirect_stderr(io.StringIO()) as output:
            with self.assertRaisesRegex(CameraError, "focus did not complete"):
                run_focus_action(camera, 22000)
        self.assertTrue(camera.closed)
        self.assertIn("Restore: restore failed", output.getvalue())
        self.assertIn("Disconnect: disconnect failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
