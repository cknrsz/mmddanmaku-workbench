@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_NO_CACHE_DIR=1"
title Yukkuri PSD to PNG setup

echo ==================================================
echo   Yukkuri PSD layer exporter
echo ==================================================
echo.

rem Put this CMD in repository root. Also supports running next to the PSDs.
set "YUKKURI_DIR=%~dp0frontend\public\yukkuri"
if not exist "%YUKKURI_DIR%\" set "YUKKURI_DIR=%~dp0."

echo Input directory: "%YUKKURI_DIR%"
if not exist "%YUKKURI_DIR%\ゆっくりチルノ.psd" goto :missing
if not exist "%YUKKURI_DIR%\ゆっくり大妖精.psd" goto :missing

rem Find any Python 3.12+ interpreter; the py launcher is optional.
rem Prefer an explicitly registered Python 3.12 when available.
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3.12"
)
if not defined PYTHON_CMD (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
  )
)
if not defined PYTHON_CMD (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python3"
)
if not defined PYTHON_CMD goto :python_missing
echo Using Python: %PYTHON_CMD%
where powershell >nul 2>nul
if errorlevel 1 goto :powershell_missing

set "WORK_DIR=%TEMP%\yukkuri-export-%RANDOM%-%RANDOM%"
if exist "%WORK_DIR%" goto :temp_failure
mkdir "%WORK_DIR%"
if errorlevel 1 goto :temp_failure
set "VENV_DIR=%WORK_DIR%\venv"
set "PY_FILE=%WORK_DIR%\export_layers.py"
set "CMD_FILE=%~f0"
set "EXIT_CODE=1"

echo.
echo [1/4] Creating temporary Python environment...
%PYTHON_CMD% -m venv "%VENV_DIR%"
if errorlevel 1 goto :cleanup

echo.
echo [2/4] Installing PSD conversion dependencies temporarily...
set "PIP_DEFAULT_TIMEOUT=120"
set "PIP_RETRIES=10"

"%VENV_DIR%\Scripts\python.exe" -m pip install --disable-pip-version-check "psd-tools[composite]"

if errorlevel 1 (
    echo.
    echo Official PyPI failed. Trying Tsinghua mirror...
    "%VENV_DIR%\Scripts\python.exe" -m pip install --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple "psd-tools[composite]"
    if errorlevel 1 goto :cleanup
)

echo.
echo [3/4] Preparing layer exporter...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$lines=[System.IO.File]::ReadAllLines($env:CMD_FILE,[System.Text.Encoding]::UTF8); $i=[Array]::IndexOf($lines, ':: PYTHON_PAYLOAD_START'); if($i -lt 0 -or $i -ge $lines.Length-1){exit 1}; [System.IO.File]::WriteAllLines($env:PY_FILE, [string[]]$lines[($i+1)..($lines.Length-1)], (New-Object System.Text.UTF8Encoding($false)))"
if errorlevel 1 goto :cleanup

echo.
echo [4/4] Exporting every PSD leaf layer to transparent PNG...
"%VENV_DIR%\Scripts\python.exe" "%PY_FILE%" "%YUKKURI_DIR%"
set "EXIT_CODE=%ERRORLEVEL%"

goto :cleanup

:missing
echo.
echo ERROR: Both PSD files are required in the input directory.
echo   - ゆっくりチルノ.psd
echo   - ゆっくり大妖精.psd
goto :failed

:python_missing
echo.
echo ERROR: No usable Python 3.12+ interpreter was found.
echo Check with: python --version
echo              py -0p
echo The Windows py launcher is optional.
goto :failed

:powershell_missing
echo.
echo ERROR: Windows PowerShell is required.
goto :failed

:temp_failure
echo.
echo ERROR: Could not create a temporary folder.
goto :failed

:cleanup
echo.
echo Cleaning temporary Python environment and downloaded libraries...
if exist "%WORK_DIR%\" rmdir /s /q "%WORK_DIR%"
if exist "%WORK_DIR%\" echo WARNING: Temporary folder could not be fully removed: "%WORK_DIR%"
if "%EXIT_CODE%"=="0" (
  echo.
  echo SUCCESS: PNG files have been generated in both character folders.
) else (
  echo.
  echo FAILED: Check the errors above. Existing PSD files were not modified.
)
echo.
pause
endlocal & exit /b %EXIT_CODE%

:failed
echo.
pause
endlocal & exit /b 1

:: PYTHON_PAYLOAD_START
# -*- coding: utf-8 -*-
"""Export individual PSD leaf layers, preserving the group hierarchy.

Unlike a flattened PSD export, this keeps all alternative eyes, mouths,
face parts and other layers, including hidden ones, for random assembly.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from psd_tools import PSDImage


CHARACTERS = ("ゆっくりチルノ", "ゆっくり大妖精")
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PRENUMBERED = re.compile(r"^\d{3,4}_.+")


def safe_name(index: int, original_name: str) -> str:
    name = INVALID_CHARS.sub("_", original_name).strip().rstrip(". ")
    name = name[:90] or "layer"
    if PRENUMBERED.match(name):
        return name
    return f"{index:03d}_{name}"


def unique_output(parent: Path, stem: str, extension: str = "") -> Path:
    return parent / f"{stem}{extension}"


def export_character(psd_file: Path, output_dir: Path) -> tuple[int, int]:
    print(f"\nReading: {psd_file.name}", flush=True)
    psd = PSDImage.open(psd_file)
    canvas = (0, 0, psd.width, psd.height)
    output_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    skipped = 0
    reserved: set[Path] = set()

    def walk(parent_layers, parent_dir: Path) -> None:
        nonlocal count, skipped
        for index, layer in enumerate(parent_layers, 1):
            stem = safe_name(index, str(layer.name or "layer"))
            if layer.is_group():
                folder = parent_dir / stem
                # Preserve empty groups too; they can be used as UI categories.
                folder.mkdir(parents=True, exist_ok=True)
                walk(layer, folder)
                continue

            target = unique_output(parent_dir, stem, ".png")
            if target in reserved:
                number = 2
                while (parent_dir / f"{stem}__{number}.png") in reserved:
                    number += 1
                target = parent_dir / f"{stem}__{number}.png"
            reserved.add(target)

            try:
                # Force all alternatives to render even if hidden in the PSD.
                image = layer.composite(viewport=canvas, layer_filter=lambda _: True)
                if image is None:
                    skipped += 1
                    print(f"  skipped (not renderable): {layer.name}")
                    continue
                image = image.convert("RGBA")
                if image.getchannel("A").getbbox() is None:
                    skipped += 1
                    print(f"  skipped (empty): {layer.name}")
                    continue
                image.save(target, format="PNG", optimize=True)
                count += 1
                if count % 25 == 0:
                    print(f"  exported {count} PNGs...", flush=True)
            except Exception as exc:
                raise RuntimeError(f"Error exporting layer {layer.name!r} -> {target}") from exc

    walk(psd, output_dir)
    print(f"DONE {output_dir.name}: {count} PNGs, {skipped} empty/unsupported layers skipped.")
    if count == 0:
        raise RuntimeError(f"No usable PNG layers were exported from {psd_file.name}")
    return count, skipped


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: export_layers.py <yukkuri_directory>")
        return 2
    root = Path(sys.argv[1])
    for name in CHARACTERS:
        if not (root / f"{name}.psd").is_file():
            print(f"Missing: {root / (name + '.psd')}")
            return 1
    total = 0
    for name in CHARACTERS:
        count, _ = export_character(root / f"{name}.psd", root / name)
        total += count
    print(f"\nFinished. Total: {total} PNG files. Original PSDs unchanged.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
