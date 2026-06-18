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

REM 2) Nuitka 打包（--include-package=src 會一併收進 _bundled_assets）
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
  --output-dir=dist ^
  --output-filename=AudioFlow.exe ^
  app.py

echo.
echo === 打包完成（若成功）：dist\AudioFlow.exe ===
echo.
echo 發佈：只需把 «dist\AudioFlow.exe» 給使用者（單一檔，不必附帶其他資料夾）。
echo 使用者端前提（無法內嵌、需自備）：Docker Desktop + WSL2 已安裝並啟動。
echo 首次啟動會在 %%LOCALAPPDATA%%\AudioFlow 自動建立 config.json / .env /
echo   docker-compose.yml / llm-service\ / openclaw\ / recordings\，並 build 容器。
echo.
echo 若 GUI 起不來（白屏/閃退），通常是 pywebview 的 WebView2 後端沒被收進去：
echo   1) 確認已安裝 Microsoft Edge WebView2 Runtime
echo   2) 視情況補旗標：--include-package=clr_loader --include-package=pythonnet
echo   3) 先用 --standalone（不加 --onefile）建 dist\app.dist\ 資料夾版除錯較快
endlocal
