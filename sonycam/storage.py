import json
import os
from pathlib import Path
import re
import shutil


def validate_scene_name(name):
    if not isinstance(name, str) or not name or name in (".", "..") or name[-1] in " .":
        raise ValueError("scene name must be a nonempty directory name without trailing spaces/dots")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise ValueError("scene name contains a path separator or an invalid filename character")
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name):
        raise ValueError("scene name is a reserved Windows filename")
    return name


def reserve_scene(root: Path, name=None):
    if name is not None:
        validate_scene_name(name)
    root.mkdir(parents=True, exist_ok=True)
    if name is not None:
        scene = root / name
        scene.mkdir()  # Atomic reservation; even an existing empty directory is refused.
        return scene
    number = max((int(p.name) for p in root.iterdir() if re.fullmatch(r"[0-9]+", p.name)), default=0) + 1
    while True:
        scene = root / str(number)
        try:
            scene.mkdir()
            return scene
        except FileExistsError:
            number += 1


def write_metadata(scene, data):
    """Create the final JSON once; never replace an existing metadata file."""
    content = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    target = scene / "metadata.json"
    with target.open("x", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def copy_exclusive(source, destination):
    # 'xb' protects against both existing files and a race after preflight.
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())

