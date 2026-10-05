"""Focus inspection and intentional lens positioning, without a shutter release."""

import sys

from .backend import cleanup_camera


def focus_info(state):
    raw = state.get("focus_distance_raw")
    infinity = raw == 0xFFFFFFFF
    finite = isinstance(raw, (int, float)) and 0 < raw < 0xFFFFFFFF
    return {
        "model": state.get("model"),
        "focus_position_raw": state.get("focus_position_raw"),
        "focus_position_unit": "normalized_0_65535_not_metres",
        "focus_driving_status_raw": state.get("focus_driving_status_raw"),
        "focus_distance_m": raw / 1000 if finite else None,
        "focus_distance_source": "camera_sdk" if finite or infinity else "unavailable",
        "focus_distance_is_infinity": infinity,
        "focus_position_property": state.get("focus_position_property"),
        "focus_distance_property": state.get("focus_distance_property"),
        "focal_length_raw": state.get("focal_length_raw"),
        "note": "If distance in metres is unavailable, record measured distances and their focus_position_raw values for reuse.",
    }


def run_focus_action(camera, target=None):
    opened = False
    result = {}
    try:
        state = camera.open()
        opened = True
        if target is not None:
            result["previous_focus_position_raw"] = state.get("focus_position_raw")
            state = camera.set_focus_position(target)
            result["requested_focus_position_raw"] = target
        result.update(focus_info(state))
    finally:
        result["cleanup_errors"] = cleanup_camera(camera, restore=opened and target is not None)
        # Also report cleanup failures when the original command raises.
        for error in result["cleanup_errors"]:
            print(f"Camera cleanup warning: {error}", file=sys.stderr)
    return result
