"""Build the native bridge with the installed x64 Visual Studio toolchain."""
import argparse
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--sdk", required=True, type=Path, help="Extracted Sony SDK directory")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
# Normalize environment key casing: some launchers supply both Path and PATH,
# which .NET Framework MSBuild refuses. This changes only child process env.
environment = {key.upper(): value for key, value in os.environ.items()}
subprocess.run(["cmake", "-S", str(root), "-B", str(root / "build"),
                "-G", "Visual Studio 17 2022", "-A", "x64",
                f"-DSONY_SDK_ROOT={args.sdk.resolve()}"], env=environment, check=True)
subprocess.run(["cmake", "--build", str(root / "build"), "--config", "Release"],
               env=environment, check=True)

