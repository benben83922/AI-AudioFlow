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
from src.bridge._worker import _worker_running

logger = logging.getLogger(__name__)


def _native_engine_ready() -> bool:
    """native 轉譯引擎（faster-whisper）是否可用。用 find_spec 探測，不實際 import
    （ctranslate2 載入很重，不該在每次輪詢環境狀態時觸發）。"""
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None

STT_CONTAINER = "whisper-stt-server"
# 此 image 只有滾動 tag（latest / latest-amd64 / cuda），沒有版本號可釘，
# 因此 pin「digest」鎖定不可變的確切 image（供應鏈最穩）。
# 這是 2026-06-18 latest 的 manifest digest（多架構，docker 會挑 amd64）。
# 要更新版本：重查 Docker Hub 取新 digest 後替換此常數。
STT_IMAGE = "hwdsl2/whisper-server@sha256:008ef78e8164be1a52d3c16a0a4702af4190bc355c057e481ceb35af739ab184"
STT_PORT = 9000

_CREATE_NO_WINDOW = 0x08000000             # Windows：不閃主控台視窗

# 錄音「當下」真正需要就緒的服務：whisper（轉譯引擎）+ STT worker（送轉譯）。
# llm-service / openclaw 是逐字稿之後的下游，不擋錄音（晚點補跑即可）。
RECORD_REQUIRED_SERVICES = ("whisper", "stt_worker")


def _popen_kwargs() -> dict:
    # Windows 下避免每次呼叫 docker 都閃出主控台視窗
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _run_docker(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
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
    _wsl_cache: tuple[float, str, str] | None = None     # (時間, state, message)

    # ── 前置依賴偵測（Docker / WSL）──

    def _wsl_state(self) -> tuple[str, str]:
        """回傳 (state, message)。state: ok | not_installed | no_distro | error。

        僅 Windows 需要 WSL（compose 服務透過 WSL 執行）；非 Windows 一律視為 ok。
        """
        if sys.platform != "win32":
            return "ok", ""

        now = time.time()
        if self._wsl_cache and now - self._wsl_cache[0] < 5.0:
            return self._wsl_cache[1], self._wsl_cache[2]

        try:
            r = subprocess.run(
                ["wsl", "-l", "-q"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, creationflags=_CREATE_NO_WINDOW,
            )
        except FileNotFoundError:
            state, msg = "not_installed", "找不到 WSL，請先安裝 WSL2（wsl --install）"
        except Exception as e:
            state, msg = "error", f"WSL 無法連線：{e}"
        else:
            # wsl -l -q 在 Windows 輸出 UTF-16，errors=replace 後會夾雜空字元，去掉再判斷
            distros = [d.strip() for d in (r.stdout or "").replace("\x00", "").splitlines() if d.strip()]
            if r.returncode != 0:
                state, msg = "not_installed", "WSL 未就緒，請確認已安裝並設定預設發行版"
            elif not distros:
                state, msg = "no_distro", "WSL 尚未安裝任何發行版（需 Ubuntu 等）"
            else:
                state, msg = "ok", ""

        self.__class__._wsl_cache = (now, state, msg)
        return state, msg

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
        """提供前端：完整錄音就緒判斷。

        錄音門檻（皆需滿足才可錄）：
            1. Docker Desktop 已安裝且執行中
            2.（Windows）WSL 就緒
            3. 有可用的音訊輸入裝置（麥克風或系統音 loopback）
            4. 四個背景服務全部 running
        任一不滿足 → can_record=False，並回傳第一個阻擋原因（blocker + message）。

        native 模式（無 Docker）：錄音門檻只看音訊裝置，轉譯由本機 faster-whisper
        in-process 完成、整理由 claude -p 事後補跑，皆不擋錄音。
        """
        if self._pipeline_mode() == "native":
            return self._environment_status_native()

        d_state, dmsg = self._docker_state()
        docker_installed = d_state != "not_installed"
        docker_running = d_state == "ok"

        w_state, wmsg = self._wsl_state()
        wsl_ok = w_state == "ok"

        audio_ok, amsg = self._audio_input_available()

        # Docker 已就緒但容器還沒起來（例如使用者啟動後才開 Docker Desktop）→ 背景重試
        if docker_running and self._stt_status in ("unknown", "error"):
            self.start_stt_async()

        # 依賴就緒才有意義去看「錄音所需服務是否啟動」
        deps_ok = docker_running and wsl_ok
        services_ready = False
        not_running = []
        if deps_ok:
            services = self.get_services_status()["data"]
            required = [services[k] for k in RECORD_REQUIRED_SERVICES if k in services]
            services_ready = all(s["running"] for s in required)
            not_running = [s["name"] for s in required if not s["running"]]

        # 依優先序找出第一個阻擋原因
        if not docker_installed:
            blocker, message = "docker_not_installed", "尚未安裝 Docker Desktop，請先安裝後再使用錄音"
        elif not docker_running:
            blocker, message = "docker_not_running", dmsg or "請先啟動 Docker Desktop"
        elif not wsl_ok:
            blocker, message = "wsl", wmsg or "WSL 未就緒"
        elif not audio_ok:
            blocker, message = "audio", amsg or "找不到可用的音訊輸入裝置"
        elif not services_ready:
            blocker, message = "services", "服務啟動中或未就緒：" + "、".join(not_running)
        else:
            blocker, message = "", ""

        can_record = deps_ok and audio_ok and services_ready

        return _ok({
            "docker_installed": docker_installed,
            "docker_running": docker_running,
            "wsl_ok": wsl_ok,
            "audio_available": audio_ok,
            "stt_status": self._stt_status,
            "services_ready": services_ready,
            "can_record": can_record,
            "blocker": blocker,
            "message": message,
        })

    def _environment_status_native(self) -> dict:
        """native 模式的錄音就緒判斷：只看音訊裝置；轉譯/整理不擋錄音。

        回傳鍵與 docker 模式對齊（前端共用），但 docker/wsl 一律視為「不適用→True」，
        避免前端跳出 Docker/WSL 安裝提示。"""
        audio_ok, amsg = self._audio_input_available()
        worker_up = _worker_running()
        engine_ok = _native_engine_ready()
        try:
            import src.claude_cli as claude_cli
            claude = claude_cli.detect()
        except Exception:
            claude = {"available": False, "mode": None, "message": ""}

        if not audio_ok:
            blocker, message = "audio", amsg or "找不到可用的音訊輸入裝置（麥克風或系統音）"
        elif not engine_ok:
            # 不擋錄音，但提示：缺 faster-whisper 則無法本機轉譯
            blocker, message = "engine", "尚未安裝 faster-whisper，無法本機轉譯（pip install faster-whisper）"
        else:
            blocker, message = "", ""

        return _ok({
            "docker_installed": True,        # native 不需 Docker；回 True 讓前端不顯示安裝提示
            "docker_running": True,
            "wsl_ok": True,
            "audio_available": audio_ok,
            "stt_status": "running" if worker_up else "stopped",
            "services_ready": worker_up,
            "can_record": audio_ok,          # 只要有音訊就能錄；轉譯/整理事後進行
            "can_summarize": claude.get("available", False),
            "claude_available": claude.get("available", False),
            "claude_mode": claude.get("mode"),
            "blocker": blocker,
            "message": message,
            "mode": "native",
        })
