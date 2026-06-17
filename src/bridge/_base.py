import json
import logging
from pathlib import Path

from src.bridge._helpers import _ok

logger = logging.getLogger(__name__)


class BridgeBase:
    def __init__(self, project_root: str):
        self._project_root = Path(project_root)
        self._window = None
        self._config_path = self._project_root / "config.json"
        self._recordings_dir = self._project_root / "recordings"
        self._recordings_dir.mkdir(exist_ok=True)

    def set_window(self, window) -> None:
        self._window = window
        logger.info("Bridge: window attached")

    def _load_config(self) -> dict:
        if self._config_path.exists():
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return self._default_config()

    def _save_config(self, config: dict) -> None:
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    # ── 三段資料夾解析（錄音 → 逐字稿 → 會議紀錄）──

    def _recordings_dir_path(self) -> Path:
        """錄音檔資料夾；未設定則用專案內建 recordings/。"""
        p = self._load_config()["storage"].get("local_path", "").strip()
        return Path(p) if p else self._recordings_dir

    def _transcripts_dir_path(self) -> Path | None:
        """逐字稿資料夾（STT worker 輸出）；未設定回 None。"""
        p = self._load_config()["storage"].get("transcripts_path", "").strip()
        return Path(p) if p else None

    def _results_dir_path(self) -> Path | None:
        """會議紀錄資料夾（openclaw 輸出）。未設定則由 transcripts_path 推導
        其同層的 openclaw-out（與 docker-compose 掛載慣例一致）。"""
        p = self._load_config()["storage"].get("results_path", "").strip()
        if p:
            return Path(p)
        t = self._load_config()["storage"].get("transcripts_path", "").strip()
        if t:
            return Path(t).parent / "openclaw-out"
        return None

    @staticmethod
    def _default_config() -> dict:
        return {
            "api_keys": {
                "whisper": "",
                "claude": "",
            },
            "storage": {
                "local_path": "",
                "transcripts_path": "",
                "results_path": "",
                "drive_folder_id": "",
                "webhook_url": "",
            },
            "distribution": {
                "google_calendar": True,
                "discord": True,
                "slack": False,
                "obsidian": True,
                "notion": False,
            },
        }

    # ── Public API ──

    def ping(self) -> dict:
        return _ok("pong")

    def get_app_version(self) -> dict:
        return _ok("0.1.0")
