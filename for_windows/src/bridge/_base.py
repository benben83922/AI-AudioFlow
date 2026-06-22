"""Bridge 基底（純 Windows 版）。

相對原系統大幅簡化：所有資料夾都在 Windows 本機（不再有 WSL 原生資料夾偵測、
不再有 docker-compose 內嵌資產寫出）。三段資料夾（錄音 / 逐字稿 / 會議紀錄）
皆位於工作根目錄下，或由使用者於設定覆寫。
"""

import os
import sys
import json
import logging
from pathlib import Path

from src.bridge._helpers import _ok

logger = logging.getLogger(__name__)


def _is_frozen() -> bool:
    """是否為打包後（Nuitka / PyInstaller）執行。"""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


class BridgeBase:
    def __init__(self, project_root: str):
        self._project_root = Path(project_root)
        # 可寫檔案（config / 錄音 / 逐字稿 / 會議紀錄 / worker log）的工作根目錄。
        # 打包後放 %LOCALAPPDATA%\AudioFlow（使用者可寫；exe 可能裝在唯讀位置，
        # onefile 暫存目錄每次重建也會遺失設定）。原始碼則用專案根。
        self._data_root = self._frozen_data_root() if _is_frozen() else self._project_root
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._window = None
        self._config_path = self._data_root / "config.json"
        self._recordings_dir = self._data_root / "recordings"
        self._recordings_dir.mkdir(exist_ok=True)

    @staticmethod
    def _frozen_data_root() -> Path:
        """打包後的工作目錄：%LOCALAPPDATA%\\AudioFlow（退而求其次用 exe 同層）。"""
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "AudioFlow"
        return Path(sys.executable).resolve().parent

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

    # ── 三段資料夾解析（皆為 Windows 本機；錄音 → 逐字稿 → 會議紀錄）──

    def _recordings_dir_path(self) -> Path:
        """錄音檔資料夾；由使用者於設定填入，未設定則用工作目錄內 recordings/。"""
        p = self._load_config()["storage"].get("local_path", "").strip()
        return Path(p) if p else self._recordings_dir

    def _transcripts_dir_path(self) -> Path:
        """逐字稿資料夾（worker 輸出 .txt）；未設定則用工作目錄內 transcripts/。"""
        p = self._load_config()["storage"].get("transcripts_path", "").strip()
        return Path(p) if p else (self._data_root / "transcripts")

    def _results_dir_path(self) -> Path:
        """會議紀錄資料夾（worker / 手動整理輸出 .md）；未設定則用工作目錄內 results/。"""
        p = self._load_config()["storage"].get("results_path", "").strip()
        return Path(p) if p else (self._data_root / "results")

    def setup_completed(self) -> bool:
        """首次設定是否已完成（決定 app 啟動時要不要直接拉 worker）。"""
        return bool(self._load_config().get("setup_done"))

    @staticmethod
    def _default_config() -> dict:
        return {
            "setup_done": False,
            "api_keys": {
                "claude": "",
            },
            "storage": {
                "local_path": "",
                "transcripts_path": "",
                "results_path": "",
            },
            "stt": {
                "model": "large-v3-turbo",
                "device": "cpu",
                "compute_type": "int8",
                "language": "zh",
            },
            "llm": {
                "model": "opus",
            },
        }

    # ── Public API ──

    def ping(self) -> dict:
        return _ok("pong")

    def get_app_version(self) -> dict:
        return _ok("0.1.0")
