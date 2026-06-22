"""AI AudioFlow（純 Windows 版）— 桌面應用程式主入口。

帶 `--worker` 旗標時不開 GUI，而是進入「內嵌處理 worker 模式」（轉譯 + 整理 pipeline）：
打包後主 exe 以 `app.exe --worker` 重新拉起自己當 worker，原始碼則
`python -m src.main --worker`。worker 實作在 src/worker_main.py。

啟動時會自動確認 / 安裝相依套件（原始碼模式、首次啟動）。重依賴一律延遲載入，
確保「確認依賴」這步能在它們被 import、可能崩潰之前先跑。GUI 與 worker 需要的
依賴不同（GUI 不需 faster-whisper），故分別確認。
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


def _ensure_deps(modules: tuple[str, ...]) -> None:
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


# worker 模式：不開 GUI。需要 faster-whisper（轉譯）；claude 走子程序、無 import 依賴。
if "--worker" in sys.argv:
    _ensure_deps(("faster_whisper",))
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
    # 隨 app 啟動：拉起背景處理 worker（轉譯 + 整理）。先判斷是否已在執行才啟動。
    # 但「首次設定」未完成前不自動拉 —— 等使用者按「完成設定」後由 complete_setup() 啟動。
    if bridge.setup_completed():
        bridge.start_all_services_async()
    else:
        logger.info("尚未完成首次設定，等待使用者於前端設定後再啟動服務")


def main() -> None:
    _ensure_deps(("webview", "sounddevice", "soundfile"))  # GUI 相依，再延遲載入 webview
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
