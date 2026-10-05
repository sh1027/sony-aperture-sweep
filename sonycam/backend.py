import json
import math
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading

from .config import ASPECTS, aperture_raw, focus_position


class CameraError(RuntimeError):
    pass


def cleanup_camera(camera, *, restore):
    """Always disconnect, preserving any earlier failure in the calling operation."""
    errors = []
    if restore:
        try:
            errors.extend(camera.restore())
        except BaseException as error:
            errors.append(f"Restore: {error}")
    try:
        camera.close()
    except BaseException as error:
        errors.append(f"Disconnect: {error}")
    return errors


class SonyCamera:
    """Keep one SDK connection alive for the entire sequence."""

    def __init__(self, config):
        self.config = config
        self.process = None
        self.responses = Queue()
        self.reader = None
        self.broken = False

    def open(self):
        if not self.config.bridge_path.is_file():
            raise CameraError(f"SDK bridge not built: {self.config.bridge_path}. See README.md.")
        index = -1 if self.config.camera_index is None else self.config.camera_index
        self.process = subprocess.Popen(
            [str(self.config.bridge_path), str(index), str(math.ceil(self.config.timeout_seconds))],
            cwd=self.config.bridge_path.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8", errors="strict", bufsize=1,
        )
        def read_responses():
            try:
                for line in self.process.stdout:
                    self.responses.put(line)
            except (OSError, UnicodeError) as error:
                self.responses.put(error)
            finally:
                self.responses.put(None)
        self.reader = threading.Thread(target=read_responses, daemon=True)
        self.reader.start()
        try:
            return self._receive(2 * self.config.timeout_seconds + 15)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass  # Preserve the original connection diagnostic.
            raise

    def _receive(self, timeout):
        try:
            line = self.responses.get(timeout=timeout)
        except Empty as error:
            self.broken = True
            raise CameraError("SDK bridge timed out; no further capture commands will be sent") from error
        if line is None or isinstance(line, Exception):
            self.broken = True
            raise CameraError(f"SDK bridge stopped unexpectedly: {line or 'EOF'}")
        try:
            response = json.loads(line)
            if not isinstance(response, dict) or "ok" not in response:
                raise ValueError("missing ok field")
        except (ValueError, TypeError) as error:
            self.broken = True
            raise CameraError(f"Invalid SDK bridge response: {line[:300]}") from error
        if not response["ok"]:
            raise CameraError(response.get("error", "Unknown SDK error"))
        return response

    def command(self, command, *, multiplier=1):
        if self.broken or self.process is None:
            raise CameraError("SDK connection is not usable")
        if "\n" in command or "\r" in command:
            raise ValueError("Line breaks are not allowed in SDK commands")
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            self.broken = True
            raise CameraError("Cannot write to SDK bridge") from error
        return self._receive(multiplier * self.config.timeout_seconds + 10)

    def prepare(self, aspect):
        return self.command(f"prepare {ASPECTS[aspect]}", multiplier=9)

    def set_aperture(self, value):
        return self.command(f"aperture {aperture_raw(value)}")

    def set_focus_position(self, value):
        return self.command(f"focus {focus_position(value)}", multiplier=3)

    def state(self):
        return self.command("state")

    def capture(self, directory):
        response = self.command("capture " + str(directory.resolve()))
        path = Path(response["file"])
        # The capture sequence checks containment before reading or copying.
        return path if path.is_absolute() else directory / path

    def restore(self):
        return self.command("restore", multiplier=11).get("restore_errors", [])

    def close(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None and not self.broken:
                self.command("quit")
        finally:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            if self.reader:
                self.reader.join(timeout=2)
            if self.process.stdout:
                self.process.stdout.close()
            self.process = None

    @staticmethod
    def list_cameras(config):
        if not config.bridge_path.is_file():
            raise CameraError(f"SDK bridge not built: {config.bridge_path}")
        result = subprocess.run([str(config.bridge_path), "--list"], cwd=config.bridge_path.parent,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        try:
            data = json.loads(result.stdout)
        except ValueError as error:
            raise CameraError(f"Cannot enumerate cameras: {result.stderr or result.stdout}") from error
        if result.returncode or not data.get("ok"):
            raise CameraError(data.get("error", "Camera enumeration failed"))
        return data["cameras"]


class MockCamera:
    """Generate clearly marked synthetic JPEGs to verify a complete run offline."""

    def __init__(self, config):
        self.aperture = 2.8
        self.aspect = "1:1"
        self.restored = False
        self.focus_position = 12345

    def open(self):
        return self.state()

    def prepare(self, aspect):
        self.aspect = aspect
        return self.state()

    def state(self):
        return {"ok": True, "model": "MOCK - synthetic image",
                "aperture": self.aperture, "supported_apertures": [2.8, 4, 5.6, 8, 11, 16, 22],
                "aspect_ratio_raw": ASPECTS[self.aspect], "focus_mode_raw": 1,
                "focus_position_raw": self.focus_position, "focus_distance_raw": 2000,
                "focus_driving_status_raw": 1,
                "focus_position_property": {"type_raw": 16386, "readable": True,
                                            "writable": True, "values_raw": [0, 65535, 1]},
                "focus_distance_property": {"readable": True, "writable": False},
                "focal_length_raw": 50000,
                "exposure_mode_raw": 3, "drive_mode_raw": 1, "file_type_raw": 1,
                "compression_raw": 1, "store_destination_raw": 1, "priority_key_raw": 2,
                "aperture_property": {"type_raw": 8194, "readable": True, "writable": True,
                                      "values_raw": [280, 400, 560, 800, 1100, 1600, 2200]}}

    def set_aperture(self, value):
        self.aperture = value
        return self.state()

    def set_focus_position(self, value):
        self.focus_position = focus_position(value)
        return self.state()

    def capture(self, directory):
        from PIL import Image, ImageDraw
        from PIL.TiffImagePlugin import IFDRational
        a, b = map(int, self.aspect.split(":"))
        image = Image.new("RGB", (a * 120, b * 120), "#19354a")
        draw = ImageDraw.Draw(image)
        draw.text((12, 12), f"MOCK / f{self.aperture}\nNot a camera photograph", fill="white")
        exif = Image.Exif()
        exif[0x0110] = "MOCK"
        exif[0x829D] = IFDRational(self.aperture)
        exif[0x920A] = IFDRational(50)
        exif[0x829A] = IFDRational(1, 125)
        exif[0x8827] = 100
        exif[0xA434] = "MOCK 50 mm"
        path = directory / "DSC00001.JPG"
        with path.open("xb") as stream:
            image.save(stream, "JPEG", exif=exif)
        return path

    def restore(self):
        self.restored = True
        return []

    def close(self):
        pass

