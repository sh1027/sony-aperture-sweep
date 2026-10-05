import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

from .backend import CameraError, MockCamera, SonyCamera
from .capture import run_sequence
from .config import Config, focus_position, positive_number
from .focus import run_focus_action


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Sony alpha 7C II: capture and save an ascending f-number sequence")
    parser.add_argument("--config", type=Path, help="JSON configuration; relative paths are resolved from this file")
    parser.add_argument("--scene", help="Scene name; defaults to the next available number in the output directory")
    parser.add_argument("--output", type=Path, help="Output directory, relative to the current working directory")
    parser.add_argument("--measured-focus-distance-m", dest="focus_distance_m", type=float,
                        help="Optional measured focus distance in metres to record; omit distance fields when unset. "
                             "This option does not move focus")
    parser.add_argument("--focus-position-raw", type=int,
                        help="Absolute focus position to set before capture: 0 to 65535, not metres")
    parser.add_argument("--camera-index", type=int, help="Camera index shown by --list-cameras")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list-cameras", action="store_true", help="List connected cameras without capturing images")
    actions.add_argument("--focus-info", action="store_true",
                         help="Show the current focus position and distance availability without capture or focus movement")
    actions.add_argument("--set-focus-position", type=int,
                         help="Move to absolute focus position 0 to 65535 and show the result without capturing images")
    parser.add_argument("--mock", action="store_true", help="Generate test JPEGs marked MOCK without using a camera")
    return parser.parse_args(argv)


def load_config(args):
    config_path = args.config
    if config_path is None and Path("config.json").is_file():
        config_path = Path("config.json")
    config = Config.load(config_path)
    if args.output is not None:
        config = replace(config, output_dir=args.output.resolve())
    if args.focus_distance_m is not None:
        config = replace(config, focus_distance_m=positive_number(args.focus_distance_m, "focus_distance_m"))
    if args.focus_position_raw is not None:
        if args.list_cameras or args.focus_info or args.set_focus_position is not None:
            raise ValueError("--focus-position-raw is a capture option; use --set-focus-position to move without capture")
        config = replace(config, focus_position_raw=focus_position(args.focus_position_raw))
    if args.set_focus_position is not None:
        focus_position(args.set_focus_position)
    if args.camera_index is not None:
        if args.camera_index < 0:
            raise ValueError("camera-index must be nonnegative")
        config = replace(config, camera_index=args.camera_index)
    return config


def main(argv=None):
    args = parse_args(argv)
    try:
        config = load_config(args)
        if args.list_cameras:
            print(json.dumps(SonyCamera.list_cameras(config), indent=2, ensure_ascii=False))
            return 0
        camera = MockCamera(config) if args.mock else SonyCamera(config)
        if args.focus_info or args.set_focus_position is not None:
            result = run_focus_action(camera, args.set_focus_position)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1 if result["cleanup_errors"] else 0
        if config.focus_distance_m is not None:
            print(f"Recording {config.focus_distance_m:g} m as the measured focus distance; this does not set focus in metres.",
                  file=sys.stderr)
        scene = run_sequence(config, camera, args.scene, simulated=args.mock)
        print(f"Saved: {scene}")
        return 0
    except KeyboardInterrupt:
        print("Operation interrupted. Any images and metadata.json already saved have been preserved.", file=sys.stderr)
        return 130
    except (ValueError, TypeError, OSError, CameraError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

