import math
from PIL import Image


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def inspect_jpeg(path):
    with Image.open(path) as image:
        if image.format not in ("JPEG", "MPO"):
            raise ValueError(f"Camera returned {image.format}, expected JPEG")
        # Sony JPEGs can contain an MPF index and an additional preview JPEG.
        # Pillow then reports MPO. Validate the full-resolution primary image
        # (frame 0); capture.py copies the original bytes without re-encoding.
        container_format = image.format
        frame_count = getattr(image, "n_frames", 1)
        image.seek(0)
        image.load()  # Detect incomplete/corrupt transfers, beyond just parsing the header.
        exif = image.getexif()
        photo = exif.get_ifd(0x8769)
        def tag(code):
            return photo.get(code, exif.get(code))
        return {
            "container_format": container_format, "frame_count": frame_count,
            "validated_frame": 0,
            "width": image.width, "height": image.height,
            "aperture": numeric(tag(0x829D)),
            "focal_length_mm": numeric(tag(0x920A)),
            "exposure_seconds": numeric(tag(0x829A)),
            "iso": numeric(tag(0x8827)),
            "camera_model": str(tag(0x0110) or ""),
            "lens_model": str(tag(0xA434) or ""),
            "datetime_original": str(tag(0x9003) or ""),
        }


def validate_image(info, aperture, aspect_ratio):
    if info["aperture"] is None or abs(info["aperture"] - aperture) > 0.051:
        raise ValueError(f"EXIF aperture mismatch: requested {aperture}, got {info['aperture']}")
    if info["focal_length_mm"] is None:
        raise ValueError("JPEG has no valid EXIF focal length; a lens reporting focal length is required")
    width, height = info["width"], info["height"]
    a, b = map(int, aspect_ratio.split(":"))
    # Some cameras store portrait pixels rotated rather than using Orientation.
    long_side, short_side = max(width, height), min(width, height)
    if abs(long_side * b - short_side * a) > max(a, b):
        raise ValueError(f"JPEG dimensions {width}x{height} do not match {aspect_ratio}")

