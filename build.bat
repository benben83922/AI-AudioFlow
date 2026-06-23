@echo off
REM ============================================================================
REM AI AudioFlow — Windows 打包腳本（Nuitka onefile，單一 exe）
REM
REM 先決條件：
REM   1) 已安裝相依：  pip install -e ".[build]"
REM   2) 在「真正的 Windows」上執行（WSL 內無法產生 Windows GUI exe）
REM   3) 首次跑 Nuitka 會自動下載 C 編譯器（MinGW），需連外網
REM
REM 產物：dist\AudioFlow.exe —— «單一檔»，內含 GUI + STT worker + Python 依賴 +
REM       compose 內容（docker-compose.yml / llm-service / openclaw 已內嵌）。
REM       發佈時「只需給這一個 exe」；app 首次啟動會把 compose 內容自動寫到
REM       %LOCALAPPDATA%\AudioFlow 並對它跑 docker compose。
REM
REM ⚠️ 尚未在 Windows 實機驗證；pywebview(WebView2/pythonnet) 的打包常需要微調，
REM    第一次建置若 GUI 起不來，多半是 webview 後端 DLL 沒被收進去——見檔末註解。
REM ============================================================================
setlocal
cd /d "%~dp0"

REM 1) 先把 compose 內容打包成 src\_bundled_assets.py（內嵌進 exe 用）
python scripts\gen_bundled_assets.py
if errorlevel 1 (
  echo [錯誤] 產生內嵌資產失敗，中止打包。
  exit /b 1
)

REM 2) Nuitka 打包（單一 exe）。收錄清單：
REM      - src（含 _bundled_assets）、前端 index.html
REM      - native 轉譯引擎：faster_whisper / ctranslate2 / onnxruntime / av / tokenizers / numpy
REM        （都是有編譯擴充 + 資料檔/DLL 的重套件，Nuitka 易漏收，必須明確收進來）
REM      - 錄音：pyaudiowpatch（WASAPI loopback）、sounddevice/soundfile 的 PortAudio/libsndfile DLL
REM      - pywebview 的 Windows(WebView2) 後端：clr_loader / pythonnet
REM    注意：caret(^) 行接續中「不可」插入 REM；註解一律放在此區塊。
python -m nuitka ^
  --standalone ^
  --onefile ^
  --assume-yes-for-downloads ^
  --windows-console-mode=disable ^
  --company-name=AudioFlow ^
  --product-name="AI AudioFlow" ^
  --file-version=0.1.0 ^
  --product-version=0.1.0 ^
  --include-package=src ^
  --include-data-dir=src/frontend=src/frontend ^
  --include-package=faster_whisper ^
  --include-package-data=faster_whisper ^
  --include-package=ctranslate2 ^
  --include-package-data=ctranslate2 ^
  --include-package=onnxruntime ^
  --include-package-data=onnxruntime ^
  --include-package=av ^
  --include-package-data=av ^
  --include-package=tokenizers ^
  --include-package=numpy ^
  --include-package=pyaudiowpatch ^
  --include-package-data=sounddevice ^
  --include-package-data=soundfile ^
  --include-package=clr_loader ^
  --include-package=pythonnet ^
  --output-dir=dist ^
  --output-filename=AudioFlow.exe ^
  app.py

echo.
echo === 打包完成（若成功）：dist\AudioFlow.exe ===
echo.
echo 發佈：只需把 «dist\AudioFlow.exe» 給使用者（單一檔）。但有兩樣「不在 exe 裡」：
echo   - Whisper 模型（~1.5GB）：不內嵌，首次轉譯時自動下載到 %%LOCALAPPDATA%%\AudioFlow\models（需連網）。
echo   - claude CLI：整理會議紀錄用，外部工具，使用者自備（app 會偵測並提示）。
echo.
echo 使用者端前提（依模式）：
echo   - native 模式（預設、無 Docker）：裝好 claude CLI；首次轉譯連網下載模型即可。
echo   - docker 模式：另需 Docker Desktop + WSL2 已安裝並啟動（compose 內容已內嵌、首次自動建容器）。
echo 首次啟動會在 %%LOCALAPPDATA%%\AudioFlow 自動建立 config.json / recordings 等工作檔。
echo.
echo 若 GUI 起不來（白屏/閃退）：多半是 pywebview 的 WebView2 後端沒收齊：
echo   1) 確認已安裝 Microsoft Edge WebView2 Runtime
echo   2) 先用 --standalone（不加 --onefile）建 dist\app.dist\ 資料夾版除錯較快
echo 若「轉逐字稿」失敗（缺 DLL/資料檔）：多半是 ctranslate2/onnxruntime/av 的資料檔沒收齊，
echo   可改用資料夾版（--standalone 不加 --onefile）確認缺哪個檔，再補 --include-package-data。
endlocal
