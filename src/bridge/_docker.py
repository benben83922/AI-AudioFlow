"""Docker / whisper 容器管理 — 偵測 Docker Desktop、隨 app 啟動 STT 容器。

錄音功能會被 Docker 可用性把關（沒裝 Docker Desktop 就不給錄），
首頁據此顯示提示。容器採 CPU 模式（模型載入系統 RAM、CPU 運算）。
"""

import os
import sys
import time
import logging
import threading
import subprocess

from src.bridge._helpers import _ok

logger = logging.getLogger(__name__)

STT_CONTAINER = "whisper-stt-server"
STT_IMAGE = "hwdsl2/whisper-server"        # CPU image；TODO: 之後 pin 具體版本
STT_PORT = 9000


def _popen_kwargs() -> dict:
    # Windows 下避免每次呼叫 docker 都閃出主控台視窗
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _run_docker(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True, text=True, timeout=timeout,
        **_popen_kwargs(),
    )


class DockerMixin:
    """Docker 偵測與 whisper 容器生命週期。"""

    # STT 容器狀態：unknown | absent | pulling | starting | running | error
    _stt_status: str = "unknown"
    _stt_message: str = ""
    _stt_lock = threading.Lock()

    # docker 狀態快取（避免頻繁輪詢 hammer docker CLI）
    _docker_cache: tuple[float, str, str] | None = None  # (時間, state, message)

    # ── Docker 偵測 ──

    def _docker_state(self) -> tuple[str, str]:
        """回傳 (state, message)。state: ok | not_installed | not_running。"""
        now = time.time()
        if self._docker_cache and now - self._docker_cache[0] < 3.0:
            return self._docker_cache[1], self._docker_cache[2]

        try:
            r = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=15)
        except FileNotFoundError:
            state, msg = "not_installed", "找不到 Docker，請安裝 Docker Desktop"
        except Exception as e:  # 逾時等
            state, msg = "not_running", f"Docker 無法連線：{e}"
        else:
            if r.returncode != 0 or not (r.stdout or "").strip():
                # 已安裝但 daemon 沒起來
                state, msg = "not_running", "Docker Desktop 尚未啟動，請先開啟 Docker Desktop"
            else:
                state, msg = "ok", ""

        self.__class__._docker_cache = (now, state, msg)
        return state, msg

    def _container_state(self) -> str:
        """absent | stopped | running。"""
        try:
            r = _run_docker(
                ["ps", "-a", "--filter", f"name=^{STT_CONTAINER}$", "--format", "{{.State}}"]
            )
        except Exception:
            return "absent"
        out = (r.stdout or "").strip()
        if not out:
            return "absent"
        return "running" if "running" in out else "stopped"

    def _stt_run_args(self) -> list[str]:
        threads = max(2, (os.cpu_count() or 4))
        return [
            "run", "-d",
            "--name", STT_CONTAINER,
            "--restart=always",
            "-p", f"{STT_PORT}:9000",
            "-v", "whisper-data:/var/lib/whisper",
            "-e", "WHISPER_MODEL=large-v3-turbo",
            "-e", "WHISPER_DEVICE=cpu",
            "-e", "WHISPER_COMPUTE_TYPE=int8",
            "-e", f"WHISPER_THREADS={threads}",
            STT_IMAGE,
        ]

    # ── 容器生命週期 ──

    def ensure_stt_container(self) -> None:
        """確保 whisper 容器在跑；必要時建立（首次會拉 image）。背景執行用。

        以非阻塞鎖把關：已有一個 ensure 在進行就直接跳過，避免定時重檢時堆疊執行緒。
        """
        if not self._stt_lock.acquire(blocking=False):
            return
        try:
            state, msg = self._docker_state()
            if state != "ok":
                self.__class__._stt_status = "error"
                self.__class__._stt_message = msg
                logger.warning("STT 無法啟動：%s", msg)
                return

            cs = self._container_state()
            if cs == "running":
                self.__class__._stt_status = "running"
                self.__class__._stt_message = ""
                return

            if cs == "stopped":
                self.__class__._stt_status = "starting"
                try:
                    _run_docker(["start", STT_CONTAINER], timeout=60)
                    self.__class__._stt_status = "running"
                    self.__class__._stt_message = ""
                    logger.info("STT 容器已啟動")
                except Exception as e:
                    self.__class__._stt_status = "error"
                    self.__class__._stt_message = f"啟動容器失敗：{e}"
                return

            # absent → 建立（首次會下載 image / 模型，較久）
            self.__class__._stt_status = "pulling"
            self.__class__._stt_message = "首次啟動：下載 whisper 映像中，請稍候…"
            logger.info("建立 STT 容器（首次需下載 image）…")
            try:
                r = _run_docker(self._stt_run_args(), timeout=900)
            except Exception as e:
                self.__class__._stt_status = "error"
                self.__class__._stt_message = f"建立容器失敗：{e}"
                return
            if r.returncode != 0:
                self.__class__._stt_status = "error"
                self.__class__._stt_message = f"建立容器失敗：{(r.stderr or '').strip()[:200]}"
                logger.error("docker run 失敗：%s", r.stderr)
                return
            self.__class__._stt_status = "running"
            self.__class__._stt_message = ""
            logger.info("STT 容器已建立並啟動")
        finally:
            self._stt_lock.release()

    def start_stt_async(self) -> None:
        """非阻塞地在背景確保 STT 容器就緒（app 啟動時呼叫）。"""
        threading.Thread(
            target=self.ensure_stt_container, name="stt-ensure", daemon=True
        ).start()

    def stop_stt_container(self) -> bool:
        """停止 whisper 容器（不移除，下次可直接 start）。"""
        try:
            r = _run_docker(["stop", STT_CONTAINER], timeout=30)
        except Exception as e:
            logger.error("停止 whisper 容器失敗：%s", e)
            return False
        if r.returncode != 0:
            # 容器本就不存在 / 已停 → 視為成功
            logger.info("docker stop 回傳非 0（可能本就未執行）：%s", (r.stderr or "").strip())
        self.__class__._stt_status = "stopped"
        self.__class__._stt_message = ""
        return True

    # ── Public API ──

    def get_environment_status(self) -> dict:
        """提供前端：Docker / STT 狀態與是否可錄音。"""
        state, dmsg = self._docker_state()
        docker_installed = state != "not_installed"
        docker_running = state == "ok"

        # Docker 已就緒但容器還沒起來（例如使用者啟動後才開 Docker Desktop）→ 背景重試
        if docker_running and self._stt_status in ("unknown", "error"):
            self.start_stt_async()

        # 規格：沒安裝 Docker Desktop 就不給錄音
        can_record = docker_installed

        if state == "not_installed":
            message = "尚未偵測到 Docker Desktop，請先安裝後再使用錄音功能"
        elif state == "not_running":
            message = dmsg  # 已安裝但未啟動
        elif self._stt_status in ("pulling", "starting"):
            message = self._stt_message or "STT 服務啟動中…"
        elif self._stt_status == "error":
            message = self._stt_message or "STT 服務啟動失敗"
        else:
            message = ""

        return _ok({
            "docker_installed": docker_installed,
            "docker_running": docker_running,
            "stt_status": self._stt_status,
            "can_record": can_record,
            "message": message,
        })
