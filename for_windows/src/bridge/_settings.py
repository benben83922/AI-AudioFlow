"""設定管理（純 Windows 版）— Claude token、資料夾、STT 參數。

相對原系統移除：webhook / Google Drive / .env 同步（無 docker-compose）。
"""

import logging

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)


def _keep(new: str, old: str) -> str:
    """空字串或遮罩（••••）不覆寫既有值，避免把已填的 token 洗掉。"""
    new = (new or "").strip()
    return old if (not new or set(new) <= {"•"}) else new


class SettingsMixin:
    """設定管理 — API 金鑰、儲存路徑、STT 參數。"""

    def load_settings(self) -> dict:
        return _ok(self._load_config())

    # ── 首次設定（啟動服務前，讓使用者先填錄音資料夾）──

    def get_setup_state(self) -> dict:
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
        """完成首次設定：存錄音資料夾與 Claude token、標記 setup_done，然後啟動處理服務。

        claude_token 可留空 —— 屆時逐字稿照常產出，只是不會自動整理（可日後補填，
        或於 Windows / WSL 以 `claude setup-token` 登入後直接生效）。
        """
        config = self._load_config()
        config["storage"]["local_path"] = local_path.strip()
        config["api_keys"]["claude"] = _keep(claude_token, config["api_keys"].get("claude", ""))
        config["setup_done"] = True
        self._save_config(config)
        logger.info("首次設定完成，錄音資料夾＝%s，Claude token %s",
                    local_path.strip() or "(預設)", "已填" if config["api_keys"]["claude"] else "未填")
        self.start_all_services_async()
        return _ok({"message": "設定完成，處理服務啟動中"})

    def save_api_keys(self, claude_key: str = "") -> dict:
        """儲存 Claude 訂閱 token。空字串或遮罩（••••）不覆寫既有值。"""
        config = self._load_config()
        config["api_keys"]["claude"] = _keep(claude_key, config["api_keys"].get("claude", ""))
        self._save_config(config)
        # token 改變後，重啟 worker 才會帶入新 token（提示由前端負責）
        logger.info("Claude token %s", "已設定" if config["api_keys"]["claude"] else "未設定")
        return _ok()

    def save_storage_settings(self, local_path: str, transcripts_path: str = "",
                              results_path: str = "") -> dict:
        config = self._load_config()
        config["storage"]["local_path"] = local_path.strip()
        config["storage"]["transcripts_path"] = transcripts_path.strip()
        config["storage"]["results_path"] = results_path.strip()
        self._save_config(config)
        logger.info("Storage settings saved")
        return _ok()

    def save_stt_settings(self, model: str = "", device: str = "",
                          compute_type: str = "", language: str = "") -> dict:
        """儲存 STT（faster-whisper）參數。需重啟處理服務才生效。"""
        config = self._load_config()
        stt = config.setdefault("stt", {})
        if model.strip():
            stt["model"] = model.strip()
        if device.strip():
            stt["device"] = device.strip()
        if compute_type.strip():
            stt["compute_type"] = compute_type.strip()
        if language.strip():
            stt["language"] = language.strip()
        self._save_config(config)
        logger.info("STT settings saved: %s", stt)
        return _ok()
