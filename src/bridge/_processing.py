import logging
from pathlib import Path

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)


# 處理步驟定義
_PIPELINE_STEPS = [
    {"key": "upload",    "name": "上傳至 Google Drive",     "description": "將 MP3 上傳至雲端儲存空間"},
    {"key": "webhook",   "name": "Webhook 觸發 Make",       "description": "自動化平台收到上傳事件"},
    {"key": "whisper",   "name": "Whisper 語音轉文字",      "description": "OpenAI Whisper API 轉譯"},
    {"key": "claude",    "name": "Claude 結構化摘要",       "description": "Claude API 生成 Markdown 摘要"},
    {"key": "calendar",  "name": "Google 日曆建立行程",     "description": "自動建立會議記錄"},
    {"key": "distribute","name": "Discord / Obsidian 分發", "description": "推送通知並備份筆記"},
]


class ProcessingMixin:
    """AI 處理 — Whisper 轉譯、Claude 摘要、進度追蹤。"""

    def get_stats(self) -> dict:
        """取得儀表板統計數字。"""
        config = self._load_config()
        local_path = config["storage"].get("local_path", "").strip()
        recordings_dir = Path(local_path) if local_path else self._recordings_dir

        recordings_today = 0
        if recordings_dir.exists():
            from datetime import datetime, date
            today = date.today()
            for p in recordings_dir.iterdir():
                if p.suffix.lower() in (".wav", ".mp3", ".m4a"):
                    if datetime.fromtimestamp(p.stat().st_mtime).date() == today:
                        recordings_today += 1

        return _ok({
            "recordings_today": recordings_today,
            "transcriptions_total": 0,
            "distributions_total": 0,
        })

    def get_pipeline_status(self) -> dict:
        """取得當前錄音的處理流程狀態（示範用）。"""
        steps = [
            {**s, "status": "pending", "detail": ""}
            for s in _PIPELINE_STEPS
        ]
        return _ok({
            "recording_name": "尚無進行中的處理",
            "steps": steps,
        })

    def retry_processing(self, filename: str) -> dict:
        """重新觸發指定錄音的處理流程。"""
        config = self._load_config()
        local_path = config["storage"].get("local_path", "").strip()
        recordings_dir = Path(local_path) if local_path else self._recordings_dir
        target = recordings_dir / filename

        if not target.exists():
            return _err(ErrorType.NOT_FOUND, f"找不到檔案：{filename}")

        webhook_url = config["storage"].get("webhook_url", "").strip()
        if not webhook_url:
            return _err(ErrorType.VALIDATION, "尚未設定 Webhook URL，請先至設定頁填寫")

        try:
            import httpx
            with httpx.Client(timeout=15) as client:
                client.post(webhook_url, json={"filename": filename, "action": "retry"})
            logger.info("Retry triggered for: %s", filename)
            return _ok()
        except Exception as e:
            return _err(ErrorType.CONNECTION_ERROR, str(e))
