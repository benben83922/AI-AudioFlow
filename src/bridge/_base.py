import os
import sys
import json
import base64
import logging
import subprocess
from pathlib import Path

from src.bridge._helpers import _ok
from src.bridge._platform import detect_platform

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000  # Windows：不閃主控台視窗


def _is_frozen() -> bool:
    """是否為打包後（Nuitka / PyInstaller）執行。"""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


class BridgeBase:
    def __init__(self, project_root: str):
        self._project_root = Path(project_root)
        # 可寫檔案（config / 錄音 / worker log）與 compose 內容的工作根目錄。
        # 打包後放到 %LOCALAPPDATA%\AudioFlow（使用者可寫；exe 可能裝在 Program Files
        # 等唯讀位置，且 onefile 暫存目錄每次重建會遺失設定）。原始碼則用專案根。
        self._data_root = self._frozen_data_root() if _is_frozen() else self._project_root
        self._data_root.mkdir(parents=True, exist_ok=True)
        # 打包模式：把內嵌的 compose 內容寫出到工作目錄，docker compose 才有實體檔可讀
        if _is_frozen():
            self._materialize_bundled_assets()
        self._window = None
        self._config_path = self._data_root / "config.json"
        self._recordings_dir = self._data_root / "recordings"
        self._recordings_dir.mkdir(exist_ok=True)
        self._wsl_dir_cache: dict[str, Path] = {}  # 自動偵測到的 WSL 原生資料夾（依裝置）

    @staticmethod
    def _frozen_data_root() -> Path:
        """打包後的工作目錄：%LOCALAPPDATA%\\AudioFlow（退而求其次用 exe 同層）。"""
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "AudioFlow"
        return Path(sys.executable).resolve().parent

    def _materialize_bundled_assets(self) -> None:
        """把內嵌 exe 的 compose 內容（docker-compose.yml / llm-service / openclaw）
        寫到工作目錄。內容變動才覆寫，避免每次啟動都寫檔。"""
        try:
            from src._bundled_assets import ASSETS
        except Exception:
            return  # 原始碼模式沒有這個模組（現場本來就有真檔，不需寫出）
        for rel, b64 in ASSETS.items():
            try:
                data = base64.b64decode(b64)
                dest = self._data_root / rel
                if dest.exists() and dest.read_bytes() == data:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            except Exception as e:
                logger.warning("寫出內嵌資產 %s 失敗：%s", rel, e)

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

    # ── WSL 原生資料夾自動偵測（依裝置）──

    def _wsl_native_dir(self, subdir: str) -> Path | None:
        r"""自動偵測「當前裝置」WSL 家目錄下的資料夾，回傳給 Windows 端用的路徑。

        app 跑在 Windows、逐字稿/結果資料夾在 WSL 原生 ext4：用 `wslpath -w` 讓 WSL
        自己回報 `\\wsl.localhost\<distro>\home\<user>\<subdir>`（distro 與使用者皆
        依當前裝置自動帶出，不寫死）。原生 Linux 則直接用 `~/<subdir>`。
        結果快取，避免每次輪詢都呼叫 wsl。
        """
        cached = self._wsl_dir_cache.get(subdir)
        if cached is not None:
            return cached

        result: Path | None = None
        if sys.platform != "win32":
            result = Path.home() / subdir
        else:
            try:
                r = subprocess.run(
                    ["wsl", "-e", "bash", "-lc",
                     f'mkdir -p "$HOME/{subdir}" && wslpath -w "$HOME/{subdir}"'],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=15, creationflags=_CREATE_NO_WINDOW,
                )
                out = (r.stdout or "").strip()
                if r.returncode == 0 and out:
                    result = Path(out)
                else:
                    logger.warning("wslpath 偵測失敗（%s）：%s", subdir, (r.stderr or "").strip())
            except Exception as e:
                logger.warning("自動偵測 WSL 路徑失敗（%s）：%s", subdir, e)

        if result is not None:
            self._wsl_dir_cache[subdir] = result
        return result

    # ── 三段資料夾解析（錄音 → 逐字稿 → 會議紀錄）──

    def _recordings_dir_path(self) -> Path:
        """錄音檔資料夾；由使用者於設定填入，未設定則用專案內建 recordings/。"""
        p = self._load_config()["storage"].get("local_path", "").strip()
        return Path(p) if p else self._recordings_dir

    def _transcripts_dir_path(self) -> Path | None:
        """逐字稿資料夾（worker 輸出）。未明確設定時：
            native 模式 → 工作目錄下的 transcripts/（本機原生，無 WSL 掛載）。
            docker 模式 → 依裝置自動偵測 WSL 家目錄下的 stt-outbox
                          （與 docker-compose 掛載的 $HOME/stt-outbox 同一處）。

        Windows：一律固定為 WSL 家目錄的 stt-outbox（忽略使用者設定，前端亦唯讀）。"""
        if detect_platform() == "windows":
            return self._wsl_native_dir("stt-outbox")
        p = self._load_config()["storage"].get("transcripts_path", "").strip()
        if p:
            return Path(p)
        if self._pipeline_mode() == "native":
            return self._data_root / "transcripts"
        return self._wsl_native_dir("stt-outbox")

    def _results_dir_path(self) -> Path | None:
        """會議紀錄資料夾（openclaw 輸出）。優先用明確設定；其次由 transcripts_path
        推導同層 openclaw-out；都沒設定則依裝置自動偵測 WSL 家目錄下的 openclaw-out
        （與 docker-compose 掛載的 $HOME/openclaw-out 同一處）。"""
        # Windows：一律固定為 WSL 家目錄的 openclaw-out（忽略使用者設定，前端亦唯讀）
        if detect_platform() == "windows":
            return self._wsl_native_dir("openclaw-out")
        p = self._load_config()["storage"].get("results_path", "").strip()
        if p:
            return Path(p)
        if self._pipeline_mode() == "native":
            return self._data_root / "results"
        t = self._load_config()["storage"].get("transcripts_path", "").strip()
        if t:
            return Path(t).parent / "openclaw-out"
        return self._wsl_native_dir("openclaw-out")

    def setup_completed(self) -> bool:
        """首次設定是否已完成（決定 app 啟動時要不要直接拉服務）。"""
        return bool(self._load_config().get("setup_done"))

    # ── 管線模式（Docker 容器 vs 本機原生）──

    def _pipeline_mode(self) -> str:
        """解析實際管線模式：回傳 'docker' 或 'native'。

        設定 pipeline.mode：
            'docker' → 一律走 whisper 容器 + llm-service/openclaw（需 Docker/WSL）。
            'native' → 一律走本機 faster-whisper + claude -p（不需 Docker）。
            'auto'（預設）→ Docker 可用就 docker，否則退回 native。
        讓既有 Windows+Docker 安裝維持原行為，純 Linux / 無 Docker 自動走原生。
        """
        mode = (self._load_config().get("pipeline", {}).get("mode", "auto") or "auto").lower()
        if mode in ("docker", "native"):
            return mode
        try:
            state, _ = self._docker_state()   # DockerMixin 提供（runtime 解析）
        except Exception:
            state = "not_installed"
        return "docker" if state == "ok" else "native"

    def _summary_prompt(self) -> str:
        """使用者自訂的「會議紀錄」prompt（空字串＝用 prompts.DEFAULT_SUMMARY_PROMPT）。"""
        return self._load_config().get("summary", {}).get("prompt", "").strip()

    def _auto_pipeline(self) -> bool:
        """是否「自動」處理整條管線（錄音→逐字稿→會議紀錄全自動）。

        預設 False＝改為每個檔案手動觸發：使用者於清單按「轉逐字稿」才轉譯、
        按「轉會議紀錄」才整理。設 pipeline.auto=true 可回到全自動。
        """
        return bool(self._load_config().get("pipeline", {}).get("auto", False))

    @staticmethod
    def _default_config() -> dict:
        return {
            "setup_done": False,
            "pipeline": {
                "mode": "auto",            # auto | docker | native
                "auto": False,             # False＝每檔手動按鈕觸發；True＝全自動
            },
            "stt": {                       # native 模式的 faster-whisper 參數
                "model": "large-v3-turbo",
                "device": "cpu",
                "compute_type": "int8",
            },
            "summary": {                   # 會議紀錄 prompt（空＝用預設）
                "prompt": "",
            },
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
