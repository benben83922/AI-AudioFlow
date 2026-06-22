"""環境就緒偵測（純 Windows 版）— 取代原系統的 Docker 把關。

純 Windows 版錄音與轉譯都在本機原生完成，**不依賴 Docker / WSL**，因此：
    - 錄音門檻只看「有沒有可用的音訊輸入裝置」。
    - Claude CLI 以雙偵測（Windows 原生優先、WSL 後備）回報，但它只影響「自動整理」，
      不擋錄音——逐字稿照常產出，缺 claude 時整理留待補跑。
"""

import logging

import src.claude_cli as claude_cli
from src.bridge._helpers import _ok
from src.bridge._worker import _worker_running

logger = logging.getLogger(__name__)


class EnvironmentMixin:
    """錄音就緒判斷與相依工具偵測。"""

    def get_environment_status(self) -> dict:
        """提供前端：錄音就緒判斷 + Claude 整理能力狀態。

        can_record 只取決於音訊裝置（STT 為本機 in-process，整理在事後）。
        claude_* 給前端顯示整理能力與安裝提示，但不影響 can_record。
        """
        audio_ok, amsg = self._audio_input_available()
        worker_up = _worker_running()
        claude = claude_cli.detect()

        if not audio_ok:
            blocker, message = "audio", amsg or "找不到可用的音訊輸入裝置（麥克風或系統音）"
        else:
            blocker, message = "", ""

        return _ok({
            "audio_available": audio_ok,
            "worker_running": worker_up,
            "claude_available": claude["available"],
            "claude_mode": claude["mode"],            # windows | wsl | None
            "claude_message": claude["message"],
            "can_record": audio_ok,
            "can_summarize": claude["available"],
            "blocker": blocker,
            "message": message,
        })

    def redetect_claude(self) -> dict:
        """前端「重新偵測」按鈕：強制重查 Claude CLI 狀態。"""
        return _ok(claude_cli.detect(force=True))
