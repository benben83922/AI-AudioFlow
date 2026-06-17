"""統一服務管理 — 把四個背景服務收斂成單一前端 API。

四個服務：
    whisper      語音轉文字容器（由 DockerMixin 管理，docker 直連）
    stt_worker   逐字稿 worker（由 WorkerMixin 管理，detached 程序）
    llm_service  OpenAI 相容外殼（docker compose，內跑 claude -p）
    openclaw     逐字稿整理（docker compose）

啟停策略：
    - 每個 start 路徑都會「先判斷是否已在執行」才動作（冪等）。
    - docker compose 的兩個服務在 Windows 上透過 WSL 執行（compose 檔用的是 WSL 路徑），
      所有「會改狀態」的 compose 指令以 _compose_lock 串行化，避免並行 up 互相打架。
    - start 一律非阻塞（build / pull 可能很久），前端靠輪詢 get_services_status 看結果。
"""

import os
import sys
import json
import time
import shlex
import logging
import threading
import subprocess
from pathlib import Path, PureWindowsPath

from src.bridge._helpers import _ok, _err, ErrorType
from src.bridge._worker import _worker_running

logger = logging.getLogger(__name__)

# 前端 key → docker compose 服務名
COMPOSE_SVC = {"llm_service": "llm-service", "openclaw": "openclaw"}

# 啟動順序（whisper / worker 先就緒，再起下游 compose）
SERVICE_ORDER = ("whisper", "stt_worker", "llm_service", "openclaw")

SERVICE_META = {
    "whisper":     {"name": "Whisper 語音引擎", "desc": "本地語音轉文字容器（port 9000）"},
    "stt_worker":  {"name": "STT Worker",       "desc": "監看錄音資料夾並送轉譯"},
    "llm_service": {"name": "LLM 服務",          "desc": "OpenAI 相容介面，後端 claude -p"},
    "openclaw":    {"name": "OpenClaw 整理",     "desc": "逐字稿 → 會議紀錄 Markdown"},
}

_CREATE_NO_WINDOW = 0x08000000  # Windows：不閃主控台視窗


class ServicesMixin:
    """四個背景服務的統一啟停與狀態查詢。"""

    _compose_lock = threading.Lock()
    _services_cache: tuple[float, dict] | None = None  # (時間, status dict)

    # ── docker compose 執行（Windows 走 WSL）──

    def _to_wsl_path(self, win_path) -> str:
        """C:\\Users\\x → /mnt/c/Users/x（僅 Windows 用）。"""
        p = PureWindowsPath(win_path)
        drive = p.drive.rstrip(":")
        rest = p.as_posix()[len(p.drive):]
        return f"/mnt/{drive.lower()}{rest}"

    def _run_compose(self, args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
        """在專案根目錄執行 docker compose；Windows 透過 WSL（compose 檔用 WSL 路徑）。"""
        if sys.platform == "win32":
            wsl_root = self._to_wsl_path(self._project_root)
            inner = "cd {} && docker compose {}".format(
                shlex.quote(wsl_root),
                " ".join(shlex.quote(a) for a in args),
            )
            cmd = ["wsl", "-e", "bash", "-lc", inner]
            kwargs = {"creationflags": _CREATE_NO_WINDOW}
        else:
            cmd = ["docker", "compose", *args]
            kwargs = {"cwd": str(self._project_root)}
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, **kwargs)

    def _compose_status_all(self) -> dict:
        """一次查回所有 compose 服務的運行狀態：{compose 服務名: running_bool}。"""
        result: dict = {}
        try:
            r = self._run_compose(["ps", "-a", "--format", "json"], timeout=40)
        except Exception as e:
            logger.warning("compose ps 失敗：%s", e)
            return result
        raw = (r.stdout or "").strip()
        if not raw:
            return result

        objs: list = []
        try:  # 新版 compose：NDJSON（一行一物件）
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    objs.append(json.loads(line))
        except json.JSONDecodeError:
            try:  # 舊版：單一 JSON 陣列
                objs = json.loads(raw)
            except Exception:
                objs = []

        for o in objs:
            svc = o.get("Service")
            state = (o.get("State") or "").lower()
            if svc:
                result[svc] = state.startswith("running") or state == "up"
        return result

    def _compose_running(self, svc: str) -> bool:
        return self._compose_status_all().get(svc, False)

    def _compose_up(self, svc: str) -> None:
        """啟動單一 compose 服務（先判斷是否已在跑）。串行化避免並行 up。"""
        with self._compose_lock:
            if self._compose_running(svc):
                logger.info("compose 服務 %s 已在執行，略過啟動", svc)
                return
            try:
                r = self._run_compose(["up", "-d", "--build", svc], timeout=1200)
                if r.returncode != 0:
                    logger.error("compose up %s 失敗：%s", svc, (r.stderr or "").strip()[:300])
                else:
                    logger.info("compose 服務 %s 已啟動", svc)
            except Exception as e:
                logger.error("compose up %s 例外：%s", svc, e)
        self.__class__._services_cache = None

    def _compose_stop(self, svc: str) -> None:
        with self._compose_lock:
            try:
                self._run_compose(["stop", svc], timeout=120)
                logger.info("compose 服務 %s 已停止", svc)
            except Exception as e:
                logger.error("compose stop %s 例外：%s", svc, e)
        self.__class__._services_cache = None

    # ── 狀態組裝 ──

    def _svc(self, key: str, *, running: bool, status: str, message: str = "") -> dict:
        meta = SERVICE_META[key]
        return {
            "key": key,
            "name": meta["name"],
            "description": meta["desc"],
            "running": running,
            "status": status,
            "message": message,
        }

    def _whisper_status(self, docker_ok: bool, docker_msg: str) -> dict:
        if not docker_ok:
            return self._svc("whisper", running=False, status="error", message=docker_msg)
        cs = self._container_state()  # absent | stopped | running
        if cs == "running":
            return self._svc("whisper", running=True, status="running")
        if self._stt_status in ("pulling", "starting"):
            return self._svc("whisper", running=False, status=self._stt_status,
                             message=self._stt_message)
        return self._svc("whisper", running=False,
                         status="stopped" if cs == "stopped" else "absent")

    # ── Public API ──

    def get_services_status(self) -> dict:
        """四個服務的狀態（含 2.5s 快取，避免輪詢頻繁打 docker / WSL）。"""
        now = time.time()
        if self._services_cache and now - self._services_cache[0] < 2.5:
            return _ok(self._services_cache[1])

        docker_state, docker_msg = self._docker_state()
        docker_ok = docker_state == "ok"

        worker_up = _worker_running()
        out: dict = {
            "whisper": self._whisper_status(docker_ok, docker_msg),
            "stt_worker": self._svc(
                "stt_worker",
                running=worker_up,
                status="running" if worker_up else "stopped",
            ),
        }

        compose = self._compose_status_all() if docker_ok else {}
        for key, svc in COMPOSE_SVC.items():
            if not docker_ok:
                out[key] = self._svc(key, running=False, status="error", message=docker_msg)
            else:
                running = compose.get(svc, False)
                out[key] = self._svc(key, running=running,
                                     status="running" if running else "stopped")

        self.__class__._services_cache = (now, out)
        return _ok(out)

    def get_system_health(self) -> dict:
        """單一健康指示：把四個服務濃縮成一句白話狀態，給側邊欄 / 服務頁用。

        level: ready（全部就緒）| starting（啟動中）| attention（需處理）
        """
        status = self.get_services_status()["data"]
        services = list(status.values())
        running = sum(1 for s in services if s["running"])
        total = len(services)
        transient = [s for s in services if s["status"] in ("starting", "pulling")]
        errored = [s for s in services if s["status"] == "error"]

        if errored:
            # Docker 沒就緒會讓全部變 error，優先回報那一句
            level, message = "attention", errored[0].get("message") or "部分服務異常，請檢查 Docker"
        elif running == total:
            level, message = "ready", "系統就緒，可開始錄音"
        elif transient:
            names = "、".join(s["name"] for s in transient)
            level, message = "starting", f"{names} 啟動中…"
        else:
            stopped = [s for s in services if not s["running"]]
            names = "、".join(s["name"] for s in stopped)
            level, message = "attention", f"{names} 未啟動"

        return _ok({
            "level": level,
            "message": message,
            "running": running,
            "total": total,
        })

    def start_service(self, key: str) -> dict:
        if key not in SERVICE_META:
            return _err(ErrorType.VALIDATION, f"未知服務：{key}")
        self.__class__._services_cache = None

        if key == "whisper":
            self.start_stt_async()                       # 內部會先判斷容器是否已在跑
        elif key == "stt_worker":
            threading.Thread(target=self.start_worker_detached,
                             name="svc-start-worker", daemon=True).start()  # 內部判斷 port 是否被占
        else:
            svc = COMPOSE_SVC[key]
            threading.Thread(target=self._compose_up, args=(svc,),
                             name=f"svc-up-{svc}", daemon=True).start()

        return _ok({"message": f"{SERVICE_META[key]['name']} 啟動中…"})

    def stop_service(self, key: str) -> dict:
        if key not in SERVICE_META:
            return _err(ErrorType.VALIDATION, f"未知服務：{key}")
        self.__class__._services_cache = None

        if key == "whisper":
            ok = self.stop_stt_container()
            return _ok({"message": "Whisper 已停止"}) if ok else _err(ErrorType.INTERNAL, "停止 Whisper 失敗")
        if key == "stt_worker":
            ok = self.stop_worker()
            return _ok({"message": "STT Worker 已停止"}) if ok else _err(ErrorType.INTERNAL, "停止 STT Worker 失敗")

        svc = COMPOSE_SVC[key]
        threading.Thread(target=self._compose_stop, args=(svc,),
                         name=f"svc-stop-{svc}", daemon=True).start()
        return _ok({"message": f"{SERVICE_META[key]['name']} 停止中…"})

    def start_all_services(self) -> dict:
        """前端「全部啟動」。"""
        self.start_all_services_async()
        return _ok({"message": "已送出全部啟動指令"})

    def stop_all_services(self) -> dict:
        """前端「全部停止」。"""
        for key in reversed(SERVICE_ORDER):
            try:
                self.stop_service(key)
            except Exception as e:
                logger.error("停止 %s 失敗：%s", key, e)
        return _ok({"message": "已送出全部停止指令"})

    def start_all_services_async(self) -> None:
        """app 啟動時呼叫：四個服務一起拉起，各自先判斷是否已在執行。"""
        def _run():
            for key in SERVICE_ORDER:
                try:
                    self.start_service(key)
                except Exception as e:
                    logger.error("啟動 %s 失敗：%s", key, e)
        threading.Thread(target=_run, name="services-startup", daemon=True).start()
