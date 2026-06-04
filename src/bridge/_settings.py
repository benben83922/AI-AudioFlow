import httpx
import logging

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)


class SettingsMixin:
    """設定管理 — API 金鑰、儲存路徑、分發目標。"""

    def load_settings(self) -> dict:
        config = self._load_config()
        return _ok(config)

    def save_api_keys(self, whisper_key: str, claude_key: str) -> dict:
        if not whisper_key.strip() or not claude_key.strip():
            return _err(ErrorType.VALIDATION, "API 金鑰不可為空")
        config = self._load_config()
        config["api_keys"]["whisper"] = whisper_key.strip()
        config["api_keys"]["claude"] = claude_key.strip()
        self._save_config(config)
        logger.info("API keys saved")
        return _ok()

    def save_storage_settings(self, local_path: str, drive_folder_id: str, webhook_url: str) -> dict:
        config = self._load_config()
        config["storage"]["local_path"] = local_path.strip()
        config["storage"]["drive_folder_id"] = drive_folder_id.strip()
        config["storage"]["webhook_url"] = webhook_url.strip()
        self._save_config(config)
        logger.info("Storage settings saved")
        return _ok()

    def save_distribution_targets(self, targets: dict) -> dict:
        config = self._load_config()
        config["distribution"].update(targets)
        self._save_config(config)
        logger.info("Distribution targets saved: %s", targets)
        return _ok()

    def test_connection(self) -> dict:
        config = self._load_config()
        webhook_url = config["storage"].get("webhook_url", "").strip()
        if not webhook_url:
            return _err(ErrorType.VALIDATION, "尚未設定 Webhook URL")
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(webhook_url)
            if resp.status_code < 500:
                return _ok({"message": f"連線成功（HTTP {resp.status_code}）"})
            return _err(ErrorType.CONNECTION_ERROR, f"伺服器回應 HTTP {resp.status_code}")
        except Exception as e:
            return _err(ErrorType.CONNECTION_ERROR, str(e))
