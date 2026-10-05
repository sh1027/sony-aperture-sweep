# Sony Aperture Sweep

Capture the same scene at multiple apertures with a Sony camera over USB. The program takes one JPEG at each aperture in ascending order while keeping the focus position fixed, and saves the images with capture metadata.

By default, it uses f/2.8, f/4, f/5.6, f/8, f/11, f/16, and f/22 with a 1:1 aspect ratio.

## Setup

### Requirements

- Windows x64 and Python 3.10 or later.
- CMake 3.20 or later and Visual Studio 2022 with the Desktop development with C++ workload.
- Sony Camera Remote SDK for Windows x64, downloaded and extracted separately. The bridge was developed with SDK 1.12.00.
- Sony camera and a lens with electronic aperture control and focal length reporting.

### Installation

Run these commands from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/build_bridge.py --sdk "C:\path\to\CrSDK_Win64"
```

The SDK directory must contain `app/CRSDK/CameraRemote_SDK.h` and `external/crsdk/Cr_Core.lib`. The build copies the required SDK runtime files to `build/Release`.

### Camera preparation

1. Select still-photo mode and **A (aperture priority)**. If the lens has an aperture ring, set it to **A**.
2. Fix the camera position, compose the scene, and focus on the subject. Keep the focus and zoom rings stationary during capture.
3. Connect the camera by USB and select **Remote Shoot (PC Remote)**. Close other camera-control applications, including Imaging Edge Remote.
4. Set ISO and white balance on the camera as needed. Shutter speed is adjusted automatically in aperture priority mode.

Check the connection without taking a photo:

```powershell
.\.venv\Scripts\python.exe -m sonycam --list-cameras
```

## Usage

Capture a sequence with the default settings:

```powershell
.\.venv\Scripts\python.exe -m sonycam
```

Choose a scene name and output directory:

```powershell
.\.venv\Scripts\python.exe -m sonycam --scene plant01 --output captures
```

When `--scene` is omitted, the next number after the largest existing numeric name is used, starting at `1`. Existing scenes and files are never overwritten.

Use `--camera-index 0` to select a camera when multiple cameras are connected. Run `--help` to list all options.

### Configuration

`config.json` is loaded automatically from the current directory. Use `--config path\to\config.json` to load another file.

```json
{
  "apertures": [2.8, 4.0, 5.6, 8.0, 11, 16, 22],
  "aspect_ratio": "1:1",
  "output_dir": "captures",
  "bridge_path": "build/Release/sonycam_bridge.exe",
  "camera_index": null,
  "settle_seconds": 0.5,
  "timeout_seconds": 60,
  "focus_distance_m": null,
  "focus_position_raw": null
}
```

Choose apertures supported by your lens; they are always captured in ascending order. Supported aspect ratios are `1:1`, `3:2`, `4:3`, and `16:9`. Timing values are in seconds; increase `timeout_seconds` for long exposures.

Relative paths in the configuration are resolved from the configuration file's directory. The `--output` path is relative to the current working directory. CLI options override their corresponding configuration values.

### Focus

The program temporarily switches to manual focus during capture. Without a target position, it keeps the current focus position.

Read the current focus position without moving the lens or taking a photo:

```powershell
.\.venv\Scripts\python.exe -m sonycam --focus-info
```

Move the lens to an absolute focus position without taking a photo:

```powershell
.\.venv\Scripts\python.exe -m sonycam --set-focus-position 25000
```

Use `--focus-position-raw 25000` or `focus_position_raw` in the configuration to move before capture. Positions range from `0` to `65535`; they are not distances in metres. A specified position is kept after the operation.

To record a separately measured subject distance:

```powershell
.\.venv\Scripts\python.exe -m sonycam --measured-focus-distance-m 0.5
```

This option, or `focus_distance_m` in the configuration, only records a measurement; it does not move the lens. When unset, distance fields are omitted from metadata. `--focus-info` shows the camera-reported distance when available, otherwise `null`.

Focal length in millimetres is read from image EXIF. Adjust zoom manually before capture.

### Output

```text
captures/
  plant01/
    f2p8.jpg
    f4p0.jpg
    f5p6.jpg
    f8p0.jpg
    f11.jpg
    f16.jpg
    f22.jpg
    metadata.json
```

JPEGs are saved without recompression. `metadata.json` records actual capture values, including aperture, focal length, aspect ratio, focus position when available, and any supplied measured distance.

Metadata is saved once after capture and camera cleanup. On an error or Ctrl+C, completed images are preserved and the partial results are saved during cleanup.

## Testing

Generate synthetic images without a camera, then run the automated tests:

```powershell
.\.venv\Scripts\python.exe -m sonycam --mock --output tmp\demo
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
ctest --test-dir build -C Release --output-on-failure
```

Mock images are marked `MOCK`, and their metadata contains `simulated: true`.
