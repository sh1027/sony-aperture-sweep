import time

from .backend import cleanup_camera
from .config import ASPECTS, aperture_filename, aperture_raw
from .images import inspect_jpeg, validate_image
from .storage import copy_exclusive, reserve_scene, write_metadata


def verify_state(state, config, aperture=None, baseline=None):
    expected = {"aspect_ratio_raw": ASPECTS[config.aspect_ratio], "focus_mode_raw": 1,
                "exposure_mode_raw": 3, "drive_mode_raw": 1, "file_type_raw": 1,
                "compression_raw": 1, "store_destination_raw": 1, "priority_key_raw": 2}
    for key, value in expected.items():
        if state.get(key) != value:
            raise ValueError(f"Camera setting changed or was not applied: {key}={state.get(key)}, expected {value}")
    if aperture is not None and (state.get("aperture") is None or
                                 aperture_raw(state["aperture"]) != aperture_raw(aperture)):
        raise ValueError(f"Aperture readback mismatch: {state.get('aperture')} != {aperture}")
    if baseline:
        for key in ("focus_position_raw", "focal_length_raw"):
            if baseline.get(key) is not None and state.get(key) != baseline[key]:
                raise ValueError(f"Focus/zoom changed during sequence: {key} {baseline[key]} -> {state.get(key)}")


def validate_apertures(state, apertures):
    prop = state.get("aperture_property")
    if not prop or not prop.get("readable"):
        raise ValueError("Aperture information is not available yet. Check the camera connection, still-photo mode, and lens mounting.")
    if not prop.get("writable"):
        current = state.get("aperture")
        detail = f" (current f/{current:g})" if isinstance(current, (int, float)) else ""
        raise ValueError(f"Cannot change aperture from the PC{detail}. Set the lens aperture ring to A, if present, "
                         "and select A (aperture priority) mode for still photos. For lenses without an aperture ring, "
                         "check electronic aperture control support and lens mounting.")
    available = state.get("supported_apertures", [])
    if not available:
        raise ValueError("The camera did not provide a list of available apertures. Check the connection and lens state.")
    supported = {aperture_raw(v) for v in available}
    missing = [v for v in apertures if aperture_raw(v) not in supported]
    if missing:
        raise ValueError(f"Lens/camera does not offer all requested apertures: {missing}; available: {available}")


def captured_values(state, image, measured_distance):
    """Select observed capture values; SDK descriptors and requested settings stay internal."""
    values = {"aspect_ratio": next(name for name, raw in ASPECTS.items()
                                   if raw == state["aspect_ratio_raw"])}
    if measured_distance is not None:
        values.update(focus_distance_m=measured_distance, focus_distance_source="user_measured",
                      focus_distance_is_infinity=False)
    if state.get("focus_position_raw") is not None:
        values["focus_position_raw"] = state["focus_position_raw"]
    for key in ("aperture", "focal_length_mm", "exposure_seconds", "iso", "camera_model",
                "lens_model", "width", "height", "datetime_original"):
        value = image.get(key)
        if value is not None and value != "":
            values[key] = value
    return values


def run_sequence(config, camera, scene_name=None, *, simulated=False, progress=print):
    scene = reserve_scene(config.output_dir, scene_name)
    metadata = {
        "schema_version": 2, "scene_name": scene.name,
        "simulated": simulated, "status": "in_progress", "captures": [],
    }
    opened = False
    row = None
    try:
        camera.open()
        opened = True
        baseline = camera.prepare(config.aspect_ratio)
        verify_state(baseline, config)
        validate_apertures(baseline, config.apertures)
        if config.focus_position_raw is not None:
            baseline = camera.set_focus_position(config.focus_position_raw)
            if baseline.get("focus_position_raw") is None or baseline.get("focus_driving_status_raw") != 1:
                raise ValueError("Focus movement did not return a stationary, readable lens position")
            verify_state(baseline, config)
        incoming = scene / ".incoming"
        incoming.mkdir()
        previous_focal_length = None
        for index, aperture in enumerate(config.apertures, start=1):
            filename = aperture_filename(aperture)
            row = {"filename": filename, "status": "in_progress"}
            metadata["captures"].append(row)
            progress(f"[{index}/{len(config.apertures)}] f/{aperture:g} -> {scene.name}/{filename}")
            camera.set_aperture(aperture)
            if config.settle_seconds:
                time.sleep(config.settle_seconds)
            before = camera.state()
            verify_state(before, config, aperture, baseline)
            stage = incoming / f"{index:03d}"
            stage.mkdir()
            # Never retry a timed-out exposure.
            received = camera.capture(stage).resolve()
            if received.parent != stage.resolve() or not received.is_file():
                raise ValueError("Capture returned a file outside this exposure's transfer directory")
            info = inspect_jpeg(received)
            # Attach exposure values only once an image has actually been received.
            row.update(captured_values(before, info, config.focus_distance_m))
            validate_image(info, aperture, config.aspect_ratio)
            verify_state(camera.state(), config, aperture, baseline)
            if previous_focal_length is not None and abs(info["focal_length_mm"] - previous_focal_length) > 0.1:
                raise ValueError("EXIF focal length changed during sequence")
            previous_focal_length = info["focal_length_mm"]
            copy_exclusive(received, scene / filename)
            row["status"] = "complete"
            received.unlink()
            stage.rmdir()
        incoming.rmdir()
        metadata["status"] = "complete"
    except BaseException as error:
        metadata["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        metadata["error"] = {"type": type(error).__name__, "message": str(error)}
        if row is not None and row["status"] != "complete":
            row["status"] = metadata["status"]
        raise
    finally:
        # Finish camera cleanup first, then save one final JSON, including failures.
        cleanup_errors = cleanup_camera(camera, restore=opened)
        if cleanup_errors:
            metadata["cleanup_errors"] = cleanup_errors
            if metadata["status"] == "complete":
                metadata["status"] = "complete_with_cleanup_errors"
        write_metadata(scene, metadata)
        for warning in cleanup_errors:
            progress(f"Camera cleanup warning: {warning}")
    return scene

