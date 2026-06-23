import httpx
import logging
import subprocess

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

    def get_storage_paths(self) -> dict:
        """提供前端資料夾設定：平台、是否鎖定，以及三段資料夾「解析後」的實際位置。

        - Windows：locked=True，逐字稿/會議紀錄固定為 WSL 家目錄 stt-outbox / openclaw-out，
          前端唯讀顯示、不可變更。
        - Linux / WSL：locked=False，三段皆可自訂（儲存時驗證三者為不同資料夾）。
        """
        from src.bridge._platform import detect_platform
        plat = detect_platform()
        st = self._load_config().get("storage", {})

        def s(p) -> str:
            return str(p) if p else ""

        return _ok({
            "platform": plat,
            "locked": plat == "windows",          # 逐字稿/會議紀錄是否唯讀
            # 解析後實際位置（給顯示用）
            "recordings": s(self._recordings_dir_path()),
            "transcripts": s(self._transcripts_dir_path()),
            "results": s(self._results_dir_path()),
            # 原始設定值（給可編輯欄位回填用；空字串代表走預設）
            "recordings_set": st.get("local_path", ""),
            "transcripts_set": st.get("transcripts_path", ""),
            "results_set": st.get("results_path", ""),
        })

    def save_storage_settings(self, local_path: str, transcripts_path: str,
                              results_path: str, drive_folder_id: str, webhook_url: str) -> dict:
        """儲存資料夾與整合設定。逐字稿/會議紀錄依平台決定可否變更：

        - Windows：忽略傳入的 transcripts/results（清空 → 解析走 WSL 預設）。
        - Linux / WSL：可自訂，但「錄音 / 逐字稿 / 會議紀錄」解析後必須是三個不同資料夾。
        """
        import os
        from src.bridge._platform import detect_platform
        plat = detect_platform()

        old = self._load_config()                 # 失敗時回滾用的快照
        config = self._load_config()
        st = config["storage"]
        st["local_path"] = (local_path or "").strip()
        st["drive_folder_id"] = (drive_folder_id or "").strip()
        st["webhook_url"] = (webhook_url or "").strip()

        if plat == "windows":
            # Windows：逐字稿/紀錄鎖定為 WSL 預設，忽略使用者輸入
            st["transcripts_path"] = ""
            st["results_path"] = ""
            self._save_config(config)
            logger.info("Storage settings saved (Windows：逐字稿/紀錄固定 WSL 預設)")
            return _ok({"message": "已儲存（逐字稿／會議紀錄在 Windows 固定為 WSL 預設位置）"})

        # Linux / WSL：可自訂 → 存檔後驗證三者解析路徑互不相同
        st["transcripts_path"] = (transcripts_path or "").strip()
        st["results_path"] = (results_path or "").strip()
        self._save_config(config)

        def norm(p) -> str:
            return os.path.normcase(os.path.normpath(os.path.abspath(str(p)))) if p else ""

        paths = [norm(self._recordings_dir_path()),
                 norm(self._transcripts_dir_path()),
                 norm(self._results_dir_path())]
        if "" in paths or len(set(paths)) < 3:
            self._save_config(old)                # 回滾，不留下衝突設定
            return _err(ErrorType.VALIDATION,
                        "錄音、逐字稿、會議紀錄必須設定為三個「不同」的資料夾")

        logger.info("Storage settings saved")
        return _ok({"message": "資料夾設定已更新"})

    # ── 會議紀錄 Prompt（可於設定頁編輯）──

    def get_summary_prompt(self) -> dict:
        """回傳使用者自訂 prompt 與內建預設（前端：自訂非空就顯示自訂，否則顯示預設）。"""
        from src.prompts import DEFAULT_SUMMARY_PROMPT
        custom = self._load_config().get("summary", {}).get("prompt", "")
        return _ok({"custom": custom, "default": DEFAULT_SUMMARY_PROMPT})

    def save_summary_prompt(self, prompt: str) -> dict:
        """儲存自訂「會議紀錄」prompt（空字串＝清除自訂、回到預設）。"""
        config = self._load_config()
        config.setdefault("summary", {})["prompt"] = (prompt or "").strip()
        self._save_config(config)
        logger.info("Summary prompt saved (%d 字)", len(config["summary"]["prompt"]))
        return _ok()

    def save_distribution_targets(self, targets: dict) -> dict:
        config = self._load_config()
        config["distribution"].update(targets)
        self._save_config(config)
        logger.info("Distribution targets saved: %s", targets)
        return _ok()

    # ── 系統相依（Linux）偵測與一鍵安裝 ──

    @staticmethod
    def _missing_system_pkgs() -> list[dict]:
        """偵測影響錄音/轉譯的缺漏系統套件（apt 名稱 + 用途）。"""
        import shutil
        import ctypes.util
        missing = []
        if shutil.which("parec") is None:          # PulseAudio 原生擷取（系統音）
            missing.append({"pkg": "pulseaudio-utils", "reason": "系統音擷取（parec）"})
        if ctypes.util.find_library("portaudio") is None:   # sounddevice 後端
            missing.append({"pkg": "libportaudio2", "reason": "麥克風／音訊裝置偵測"})
        return missing

    def get_system_deps(self) -> dict:
        """偵測 Linux 缺少的系統套件。Windows 回 supported=False（不適用此機制）。"""
        import shutil
        from src.bridge._platform import detect_platform
        if detect_platform() == "windows":
            return _ok({"platform": "windows", "supported": False, "missing": [], "can_auto": False})
        missing = self._missing_system_pkgs()
        has_apt = shutil.which("apt-get") is not None
        has_pkexec = shutil.which("pkexec") is not None
        return _ok({
            "platform": detect_platform(),
            "supported": has_apt,                 # 有 apt 才支援此一鍵機制
            "missing": missing,
            "can_auto": has_apt and has_pkexec,   # 還要有 pkexec 才能跳密碼框自動裝
        })

    def install_system_deps(self) -> dict:
        """以 pkexec（跳系統密碼框）執行 apt 安裝缺少的系統套件。失敗回傳手動指令。"""
        import shutil
        from src.bridge._platform import detect_platform
        if detect_platform() == "windows":
            return _err(ErrorType.VALIDATION, "Windows 不適用此安裝方式")

        pkgs = [m["pkg"] for m in self._missing_system_pkgs()]
        if not pkgs:
            return _ok({"message": "系統相依都已安裝"})

        manual = "sudo apt install -y " + " ".join(pkgs)
        if shutil.which("apt-get") is None or shutil.which("pkexec") is None:
            return _err(ErrorType.INTERNAL, f"此環境無 pkexec/apt，請於終端機手動執行：{manual}")

        inner = "apt-get update && apt-get install -y " + " ".join(pkgs)
        logger.info("pkexec apt 安裝：%s", pkgs)
        try:
            r = subprocess.run(["pkexec", "bash", "-c", inner],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=900)
        except Exception as e:
            return _err(ErrorType.INTERNAL, f"安裝執行失敗：{e}。可手動執行：{manual}")
        if r.returncode != 0:
            # 126/127 多為使用者取消授權或找不到 pkexec
            err = (r.stderr or "").strip()[:200] or "授權被取消或權限不足"
            return _err(ErrorType.INTERNAL, f"安裝未完成：{err}。可手動執行：{manual}")
        return _ok({"message": "已安裝：" + " ".join(pkgs)})

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
