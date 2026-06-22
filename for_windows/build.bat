@echo off
REM ============================================================================
REM AI AudioFlow (pure-Windows) - Nuitka onefile build => single AudioFlow.exe
REM
REM Prereqs:
REM   1) deps installed:  pip install -e ".[build]"
REM   2) run on real Windows (or call Windows python.exe from WSL)
REM   3) first Nuitka run downloads MinGW (needs internet)
REM   4) Nuitka MUST be 2.4.x (pinned in pyproject [build]); 2.8.x/4.x crash with
REM      'assert micro_passes == 0'
REM   5) Acronis Active Protection deletes the onefile exe on creation. Exclude
REM      this folder in Acronis, or build on a machine/CI without Acronis
REM      (e.g. GitHub Actions windows-latest).
REM
REM Output: for_windows\AudioFlow.exe (single file: GUI + worker + faster-whisper)
REM Not bundled (by design):
REM   - Whisper model (~1.5GB): downloaded on first transcription to
REM     %LOCALAPPDATA%\AudioFlow\models  (ship that folder for zero-download)
REM   - Claude CLI: user-installed (Windows or WSL); detected at startup
REM ============================================================================
setlocal
cd /d "%~dp0"

echo [1/2] Nuitka building (faster-whisper/ctranslate2/av is large; first run pulls MinGW; may take tens of minutes)...

python -m nuitka ^
  --standalone --onefile --assume-yes-for-downloads ^
  --windows-console-mode=disable ^
  --company-name=AudioFlow --product-name="AI AudioFlow" ^
  --file-version=0.1.0 --product-version=0.1.0 ^
  --include-package=src ^
  --include-package=faster_whisper ^
  --include-package=ctranslate2 ^
  --include-package=av ^
  --include-package=tokenizers ^
  --include-package=huggingface_hub ^
  --include-package=sounddevice ^
  --include-package=soundfile ^
  --include-package=pyaudiowpatch ^
  --include-package=clr_loader ^
  --include-package=pythonnet ^
  --include-package-data=faster_whisper ^
  --include-package-data=ctranslate2 ^
  --nofollow-import-to=onnxruntime ^
  --nofollow-import-to=PIL ^
  --include-data-dir=src/frontend=src/frontend ^
  --output-dir=dist --output-filename=AudioFlow.exe ^
  app.py
if errorlevel 1 (
  echo [ERROR] Nuitka build failed.
  exit /b 1
)

echo [2/2] Copying single exe to for_windows\AudioFlow.exe ...
copy /Y dist\AudioFlow.exe AudioFlow.exe >nul

echo.
echo === DONE: for_windows\AudioFlow.exe (single file) ===
echo Model auto-downloads on first transcription to %%LOCALAPPDATA%%\AudioFlow\models.
echo Claude CLI must be installed by the user (detected at startup).
echo If GUI fails to open (white/crash): install Microsoft Edge WebView2 Runtime.
endlocal
