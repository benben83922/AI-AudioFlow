#!/usr/bin/env bash
# ============================================================================
# AI AudioFlow — Linux 打包腳本（Nuitka onefile，單一執行檔）
#
# 先決條件：
#   1) 安裝相依（含打包工具）：
#        pip install -e ".[build]"
#      系統 Python 受 PEP 668 擋的話，改用 venv，或：
#        pip install -e ".[build]" --break-system-packages --user
#   2) Nuitka onefile 在 Linux 需要 patchelf（已列在 [build] extras）。
#   3) 首次跑 Nuitka 會自動下載 C 編譯器，需連外網。
#
# 產物：dist/AudioFlow.bin —— 單一執行檔（chmod +x 後執行）。
#
# ⚠️ Linux 的 GTK/WebKit 與系統深度整合，「無法完全塞進 binary」；
#    目標機仍需安裝下列系統套件（見檔末「目標機需求」）才跑得起來。
#    本腳本尚未在多種發行版實機驗證，第一次建議先用資料夾版（拿掉 --onefile）除錯。
# ============================================================================
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "[1/2] 產生內嵌資產（compose 內容，供 docker 模式；native 模式用不到但無妨）…"
"$PY" scripts/gen_bundled_assets.py

echo "[2/2] Nuitka 打包（單一執行檔）…"
export CC=gcc CXX=g++
# 收錄重點：
#   - src（含 _bundled_assets）、前端
#   - native 轉譯引擎：faster_whisper / ctranslate2 / onnxruntime / av / tokenizers / numpy
#   - 錄音：sounddevice / soundfile
#   - GUI：PyQt6（QtWebEngine）用 --enable-plugin=pyqt6 自動收齊
#     GTK/WebKit2GTK 與系統深度整合無法內嵌，改用可打包的 PyQt6 後端。
# 不收：pyaudiowpatch / clr_loader / pythonnet（Windows 專用）
"$PY" -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --static-libpython=no \
  --enable-plugin=pyqt6 \
  --include-package=src \
  --include-data-dir=src/frontend=src/frontend \
  --include-package=faster_whisper \
  --include-package-data=faster_whisper \
  --include-package=ctranslate2 \
  --include-package-data=ctranslate2 \
  --include-package=onnxruntime \
  --include-package-data=onnxruntime \
  --include-package=av \
  --include-package-data=av \
  --include-package=tokenizers \
  --include-package=numpy \
  --include-package=sounddevice \
  --include-package=soundfile \
  --nofollow-import-to=torch \
  --nofollow-import-to=torchaudio \
  --nofollow-import-to=torchvision \
  --nofollow-import-to=nvidia \
  --nofollow-import-to=triton \
  --nofollow-import-to=transformers \
  --nofollow-import-to=diffusers \
  --nofollow-import-to=accelerate \
  --nofollow-import-to=sympy \
  --nofollow-import-to=mpmath \
  --nofollow-import-to=pytest \
  --nofollow-import-to=_pytest \
  --nofollow-import-to=setuptools \
  --nofollow-import-to=pkg_resources \
  --nofollow-import-to=distutils \
  --nofollow-import-to=unittest \
  --nofollow-import-to=matplotlib \
  --nofollow-import-to=scipy \
  --nofollow-import-to=pandas \
  --nofollow-import-to=IPython \
  --nofollow-import-to=jupyter \
  --nofollow-import-to=notebook \
  --output-dir=dist \
  --output-filename=AudioFlow.bin \
  app.py

echo
echo "=== 打包完成（若成功）：dist/AudioFlow.bin ==="
echo
cat <<'NOTE'
發佈：給使用者 dist/AudioFlow.bin（單一檔；chmod +x dist/AudioFlow.bin 後執行）。

「不在 binary 裡」的東西：
  - Whisper 模型（~1.5GB）：不內嵌，首次轉譯自動下載到工作目錄 models/（需連網）。
  - claude CLI：整理會議紀錄用，外部自備（app 會偵測並提示）。

== 目標機需求：仍需安裝的系統套件（GTK/WebKit/音訊與系統深度整合，無法內嵌）==
  Debian / Ubuntu：
    sudo apt install -y gir1.2-webkit2-4.1 libgtk-3-0 libportaudio2 pulseaudio-utils libsndfile1
  各自用途：
    - gir1.2-webkit2-4.1（或 4.0）+ libgtk-3-0 ... pywebview 的 GUI 後端（缺了開不了視窗）
    - libportaudio2 ........................... sounddevice（音訊裝置偵測 / 麥克風）
    - pulseaudio-utils ........................ parec（系統音擷取；缺了錄音退回只錄麥克風）
    - libsndfile1 ............................. soundfile（多數 wheel 已內含，缺了再補）
  app 的「設定」頁也會偵測 libportaudio2 / pulseaudio-utils 缺漏並提供一鍵複製安裝指令。

== 除錯（第一次打包多半要微調）==
  - GUI 起不來（WebKit/gi 相關）：先用「資料夾版」——把上面指令的 --onefile 拿掉重跑，
    產物在 dist/app.dist/AudioFlow.bin，錯誤訊息較清楚；多半是目標機缺 gir1.2-webkit2-4.1。
  - 「轉逐字稿」缺 .so / 資料檔：同樣先用資料夾版確認缺哪個檔，再補對應 --include-package-data。
NOTE
