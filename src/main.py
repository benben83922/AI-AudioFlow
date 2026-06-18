"""AI AudioFlow — 桌面應用程式進入點。

帶 `--worker` 旗標時不開 GUI，而是進入「內嵌 STT worker 模式」：
打包後主 exe 以 `app.exe --worker` 重新拉起自己當 worker，原始碼則
`python -m src.main --worker`。worker 實作在 src/worker_main.py。

啟動時會自動確認 / 安裝相依套件（原始碼模式、首次啟動），使用者不必先手動
`pip install -e .`。重依賴（webview 等）一律延遲載入，確保「確認依賴」這步
能在它們被 import、可能崩潰之前先跑。
"""

import os
import sys
import logging
import subprocess
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _is_frozen() -> bool:
    """打包後（Nuitka / PyInstaller）依賴已內嵌，不需也不該 pip install。"""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _ensure_deps(modules: tuple[str, ...] = ("webview", "sounddevice", "soundfile", "httpx")) -> None:
    """確認執行所需套件，缺了就以當前直譯器自動 `pip install -e .`（僅原始碼模式）。

    必須在 import 這些重依賴「之前」呼叫——否則 import 失敗會直接讓程式崩潰。
    """
    if _is_frozen():
        return
    try:
        for m in modules:
            __import__(m)
        return                      # 都在 → 無需安裝
    except ImportError:
        pass

    root = Path(__file__).resolve().parent.parent
    print("首次啟動：偵測到缺少相依套件，正在自動安裝（僅第一次，需連網，請稍候）…", flush=True)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--disable-pip-version-check"],
            cwd=str(root),
        )
    except Exception as e:
        print(f"自動安裝失敗：{e}\n請手動執行：pip install -e .", flush=True)
        sys.exit(1)
    if r.returncode != 0:
        print("自動安裝失敗，請手動執行：pip install -e .", flush=True)
        sys.exit(1)
    print("相依套件安裝完成，繼續啟動…", flush=True)


# worker 模式：不開 GUI，只需輕量依賴（httpx）。在 import worker 前先確認。
if "--worker" in sys.argv:
    _ensure_deps(("httpx",))
    try:
        from src.worker_main import main as _worker_main
    except ImportError:
        from worker_main import main as _worker_main  # 打包入口為頂層模組時
    raise SystemExit(_worker_main())


def _on_started(bridge, window) -> None:
    bridge.set_window(window)
    logger.info("AudioFlow: window ready")
    # 攔截視窗關閉：先跳「確認關閉」彈窗，確認後停服務再真正關閉（見 on_window_closing）。
    try:
        window.events.closing += bridge.on_window_closing
    except Exception as e:
        logger.warning("無法註冊關閉攔截：%s", e)
    # 隨 app 啟動：四個背景服務一起拉起（whisper 容器、STT worker、
    # llm-service、openclaw）。每個服務各自會先判斷是否已在執行才啟動。
    # 啟動皆為非阻塞，狀態由前端「服務管理」頁輪詢顯示。
    # 但「首次設定」未完成前不自動拉 —— 等使用者於前端填好錄音資料夾、
    # 按「完成設定」後，由 complete_setup() 才啟動服務。
    if bridge.setup_completed():
        bridge.start_all_services_async()
    else:
        logger.info("尚未完成首次設定，等待使用者於前端設定後再啟動服務")


def main() -> None:
    _ensure_deps()                 # 確認 / 安裝 GUI 相依，再延遲載入 webview
    import webview

    from src.bridge import Bridge

    project_root = Path(__file__).parent.parent
    bridge = Bridge(project_root=str(project_root))
    frontend_path = Path(__file__).parent / "frontend" / "index.html"

    logger.info("Loading frontend: %s", frontend_path)

    window = webview.create_window(
        title="AI AudioFlow",
        url=str(frontend_path),
        js_api=bridge,
        width=1280,
        height=800,
        resizable=True,
        min_size=(960, 640),
    )

    debug = os.environ.get("AUDIOFLOW_DEBUG", "0").lower() not in ("0", "false")
    webview.start(lambda: _on_started(bridge, window), debug=debug)


if __name__ == "__main__":
    main()
