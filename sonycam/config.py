from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path


ASPECTS = {"3:2": 1, "16:9": 2, "4:3": 3, "1:1": 4}


def positive_number(value, name, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return float(value)


def aperture_raw(value):
    if isinstance(value, bool):
        raise ValueError("apertures must contain numbers")
    try:
        number = Decimal(str(value))
        raw = number * 100
        if not number.is_finite() or not 0 < raw < 65535 or raw != raw.to_integral_value():
            raise ValueError("Each aperture must be positive, below 655.35 and have at most 2 decimal places")
        return int(raw)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid aperture: {value!r}") from exc


def aperture_filename(value):
    whole, fraction = divmod(aperture_raw(value), 100)
    label = str(whole)
    if fraction:
        label += "p" + f"{fraction:02d}".rstrip("0")
    elif whole < 10:
        label += "p0"
    return f"f{label}.jpg"


def focus_position(value):
    if type(value) is not int or not 0 <= value <= 65535:
        raise ValueError("focus_position_raw must be an integer from 0 to 65535 (not metres)")
    return value


@dataclass(frozen=True)
class Config:
    apertures: tuple = (2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0)
    aspect_ratio: str = "1:1"
    output_dir: Path = Path("captures")
    bridge_path: Path = Path("build/Release/sonycam_bridge.exe")
    camera_index: int | None = None
    settle_seconds: float = 0.5
    timeout_seconds: float = 60.0
    focus_distance_m: float | None = None
    focus_position_raw: int | None = None

    @classmethod
    def load(cls, path=None):
        data = {} if path is None else json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
        base = Path.cwd() if path is None else Path(path).resolve().parent
        config = cls(**data)
        if not isinstance(config.apertures, (list, tuple)) or not config.apertures:
            raise ValueError("apertures must be a nonempty array")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in config.apertures):
            raise ValueError("apertures must contain numbers")
        values = [aperture_raw(v) for v in config.apertures]
        if len(values) != len(set(values)):
            raise ValueError("Duplicate apertures are not allowed")
        if not isinstance(config.aspect_ratio, str) or config.aspect_ratio not in ASPECTS:
            raise ValueError("aspect_ratio must be 1:1, 3:2, 4:3 or 16:9")
        if config.camera_index is not None and (type(config.camera_index) is not int or config.camera_index < 0):
            raise ValueError("camera_index must be null or a nonnegative integer")
        distance = config.focus_distance_m
        if distance is not None:
            distance = positive_number(distance, "focus_distance_m")
        if config.focus_position_raw is not None:
            focus_position(config.focus_position_raw)
        for key in ("output_dir", "bridge_path"):
            value = getattr(config, key)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"{key} must be a nonempty path")
        timeout = positive_number(config.timeout_seconds, "timeout_seconds")
        if timeout < 5:
            raise ValueError("timeout_seconds must be at least 5")
        return replace(config, apertures=tuple(v / 100 for v in sorted(values)),
                       output_dir=(base / config.output_dir).resolve(),
                       bridge_path=(base / config.bridge_path).resolve(),
                       settle_seconds=positive_number(config.settle_seconds, "settle_seconds", zero=True),
                       timeout_seconds=timeout, focus_distance_m=distance)

