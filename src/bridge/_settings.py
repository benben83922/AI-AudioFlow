import httpx
import logging

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)


class SettingsMixin:
    """設定管理 — API 金鑰、儲存路徑、分發目標。"""

    def load_settings(self) -> dict:
        config = self._load_config()
        return _ok(config)

    # ── 首次設定（啟動服務前，讓使用者先填錄音資料夾）──

    def get_setup_state(self) -> dict:
        """前端首屏用：是否已完成首次設定、目前/預設的錄音資料夾。"""
        config = self._load_config()
        return _ok({
            "configured": bool(config.get("setup_done")),
            "local_path": config["storage"].get("local_path", ""),
            "default_recordings_dir": str(self._recordings_dir),
        })

    def pick_folder(self) -> dict:
        """開原生資料夾選擇對話框，回傳選到的路徑（取消回空字串）。"""
        try:
            import webview
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if result:
                return _ok(result[0])
        except Exception as e:
            return _err(ErrorType.INTERNAL, f"開啟資料夾選擇失敗：{e}")
        return _ok("")

    def complete_setup(self, local_path: str, claude_token: str = "") -> dict:
        """完成首次設定：存錄音資料夾與 Claude 訂閱 token、標記 setup_done，然後才啟動服務。

        claude_token 可留空 —— 屆時 LLM／OpenClaw 不會啟動（可日後在設定補），
        但 whisper + worker（錄音→逐字稿）照常運作。
        """
        config = self._load_config()
        config["storage"]["local_path"] = local_path.strip()
        token = (claude_token or "").strip()
        # 非空、且不是「全是遮罩圓點」才存（避免把 •••• 當成 token）
        if token and set(token) != {"•"}:
            config["api_keys"]["claude"] = token
        config["setup_done"] = True
        self._save_config(config)
        self._sync_claude_token_env()     # 同步 token 到 .env
        logger.info("首次設定完成，錄音資料夾＝%s，Claude token %s",
                    local_path.strip() or "(預設)", "已填" if config["api_keys"]["claude"] else "未填")
        self.start_all_services_async()   # 設定完才拉服務（無 token 則 LLM/openclaw 會被擋）
        return _ok({"message": "設定完成，服務啟動中"})

    def save_api_keys(self, whisper_key: str, claude_key: str) -> dict:
        """儲存金鑰 / token。空字串或遮罩（••••）不覆寫既有值，避免把已填的 token 洗掉。

        claude_key = Claude 訂閱 token（CLAUDE_CODE_OAUTH_TOKEN，claude setup-token 取得），
        是 LLM 服務啟動的必要條件。whisper_key 目前流程未用到（whisper 容器未設 API key），
        保留欄位但非必填。
        """
        def _keep(new: str, old: str) -> str:
            new = (new or "").strip()
            return old if (not new or set(new) <= {"•"}) else new

        config = self._load_config()
        config["api_keys"]["whisper"] = _keep(whisper_key, config["api_keys"].get("whisper", ""))
        config["api_keys"]["claude"] = _keep(claude_key, config["api_keys"].get("claude", ""))
        self._save_config(config)
        self._sync_claude_token_env()   # 立即同步 .env（含清空時移除舊 token）
        logger.info("API keys saved（claude token %s）", "已設定" if config["api_keys"]["claude"] else "未設定")
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
