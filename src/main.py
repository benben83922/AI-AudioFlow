"""AI AudioFlow — 桌面應用程式進入點。"""

import os
import logging
from pathlib import Path

import webview

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _on_started(bridge, window) -> None:
    bridge.set_window(window)
    logger.info("AudioFlow: window ready")
    # 隨 app 啟動：四個背景服務一起拉起（whisper 容器、STT worker、
    # llm-service、openclaw）。每個服務各自會先判斷是否已在執行才啟動。
    # 啟動皆為非阻塞，狀態由前端「服務管理」頁輪詢顯示。
    bridge.start_all_services_async()


def main() -> None:
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
