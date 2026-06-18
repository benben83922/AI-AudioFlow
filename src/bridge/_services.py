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

# 「系統就緒」只看核心服務（錄音→逐字稿）。LLM／openclaw 需使用者填 Claude token
# 才會啟動，未填屬正常狀態，不應讓整體健康卡在「未就緒」，故不納入核心。
CORE_SERVICES = ("whisper", "stt_worker")

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
    _shutting_down = False  # 關閉流程已確認 → 放行視窗關閉

    # ── docker compose 執行（Windows 走 WSL）──

    def _to_wsl_path(self, win_path) -> str:
        """C:\\Users\\x → /mnt/c/Users/x（僅 Windows 用）。"""
        p = PureWindowsPath(win_path)
        drive = p.drive.rstrip(":")
        rest = p.as_posix()[len(p.drive):]
        return f"/mnt/{drive.lower()}{rest}"

    def _run_compose(self, args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
        """在專案根目錄執行 docker compose；Windows 透過 WSL（compose 檔用 WSL 路徑）。"""
        # compose 內容（docker-compose.yml + 各 build context）位於 _data_root：
        # 原始碼 = 專案根；打包後 = exe 同層（發佈時一併放置）
        compose_root = getattr(self, "_data_root", self._project_root)
        if sys.platform == "win32":
            wsl_root = self._to_wsl_path(compose_root)
            # 在 WSL login shell 內執行：$HOME / uid / gid 皆由當前裝置自動帶出，
            # 供 compose 內插（掛載路徑 ${HOME}/... 與 ${HOST_UID}/${HOST_GID}）
            inner = "cd {} && export HOST_UID=$(id -u) HOST_GID=$(id -g) && docker compose {}".format(
                shlex.quote(wsl_root),
                " ".join(shlex.quote(a) for a in args),
            )
            cmd = ["wsl", "-e", "bash", "-lc", inner]
            kwargs = {"creationflags": _CREATE_NO_WINDOW}
        else:
            cmd = ["docker", "compose", *args]
            env = dict(os.environ)
            env.setdefault("HOST_UID", str(os.getuid()))
            env.setdefault("HOST_GID", str(os.getgid()))
            kwargs = {"cwd": str(compose_root), "env": env}
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

    def _claude_token(self) -> str:
        return self._load_config().get("api_keys", {}).get("claude", "").strip()

    def _sync_claude_token_env(self) -> None:
        """讓 .env 的 CLAUDE_CODE_OAUTH_TOKEN 與設定一致（供 docker compose 自動載入）。

        - 設定有 token → 寫入/更新該行（保留 .env 其他行）。
        - 設定清空 token → 移除該行（避免殘留舊 token）。
        """
        token = self._claude_token()
        env_path = getattr(self, "_data_root", self._project_root) / ".env"

        existing = []
        if env_path.exists():
            try:
                existing = env_path.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                logger.warning("讀取 .env 失敗：%s", e)
                existing = []

        # 先移除舊的 token 行，再視情況補上新的
        lines = [l for l in existing if not l.strip().startswith("CLAUDE_CODE_OAUTH_TOKEN=")]
        if token:
            lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}")

        # token 為空、且 .env 本來就不存在 → 不要憑空建立空檔
        if not lines and not env_path.exists():
            return
        try:
            env_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        except Exception as e:
            logger.error("寫入 .env 失敗：%s", e)

    def _compose_up(self, svc: str) -> None:
        """啟動單一 compose 服務（先判斷是否已在跑）。串行化避免並行 up。"""
        self._sync_claude_token_env()   # 起 compose 前確保 token 已寫入 .env
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

        # 「就緒」只看核心服務（whisper + worker）；LLM／openclaw 需 token 才啟動，
        # 未填 token 時它們未啟動屬正常，不應拖累整體健康。
        core = [status[k] for k in CORE_SERVICES if k in status]
        core_errored = [s for s in core if s["status"] == "error"]
        core_transient = [s for s in core if s["status"] in ("starting", "pulling")]
        core_stopped = [s for s in core if not s["running"]]

        if core_errored:
            # Docker 沒就緒會讓核心服務變 error，優先回報那一句
            level, message = "attention", core_errored[0].get("message") or "核心服務異常，請檢查 Docker"
        elif not core_stopped:
            # 核心都在跑 → 就緒；若 LLM／openclaw 未跑，附註提醒（不影響可錄音）
            down_extra = [s["name"] for s in services
                          if s["key"] not in CORE_SERVICES and not s["running"]]
            if down_extra:
                message = "可錄音；" + "、".join(down_extra) + " 未啟動（需填 Claude Token）"
            else:
                message = "系統就緒，可開始錄音"
            level = "ready"
        elif core_transient:
            names = "、".join(s["name"] for s in core_transient)
            level, message = "starting", f"{names} 啟動中…"
        else:
            names = "、".join(s["name"] for s in core_stopped)
            level, message = "attention", f"{names} 未啟動"

        return _ok({
            "level": level,
            "message": message,
            "running": running,
            "total": total,
        })

    def _service_deps_ok(self, key: str) -> tuple[bool, str]:
        """啟動某服務前，檢查其前置依賴是否就緒。回傳 (ok, message)。

        - whisper / llm_service / openclaw：需 Docker 已安裝且執行中。
        - llm_service / openclaw（compose 服務）：Windows 下另需 WSL 就緒。
        - stt_worker：無硬性前置依賴（可獨立拉起；轉譯需 whisper，但不擋啟動）。
        """
        if key == "stt_worker":
            return True, ""

        d_state, dmsg = self._docker_state()
        if d_state == "not_installed":
            return False, "尚未安裝 Docker Desktop，無法啟動此服務"
        if d_state != "ok":
            return False, dmsg or "請先啟動 Docker Desktop"

        if key in ("llm_service", "openclaw"):
            w_state, wmsg = self._wsl_state()
            if w_state != "ok":
                return False, wmsg or "WSL 未就緒，無法啟動此服務"
            # LLM 需 Claude 訂閱 token；openclaw 依賴 LLM，故同樣需要
            if not self._claude_token():
                return False, "請先在設定填入 Claude 訂閱 Token（claude setup-token）才能啟動 LLM／OpenClaw"

        return True, ""

    def start_service(self, key: str) -> dict:
        if key not in SERVICE_META:
            return _err(ErrorType.VALIDATION, f"未知服務：{key}")

        ok, msg = self._service_deps_ok(key)
        if not ok:
            return _err(ErrorType.DOCKER_UNAVAILABLE, msg)

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

    # ── 關閉生命週期 ──

    def on_window_closing(self, *args) -> bool:
        """pywebview 視窗關閉事件處理（*args 容忍不同版本的呼叫慣例）。

        首次關閉：取消這次關閉（回傳 False），改叫前端跳「確認關閉」彈窗。
        確認後 shutdown_app() 會設 _shutting_down 並停服務、再 destroy →
        destroy 觸發的這次事件就放行（回傳 True）。
        """
        if self.__class__._shutting_down:
            return True
        try:
            if self._window:
                self._window.evaluate_js("window.__confirmShutdown && window.__confirmShutdown()")
        except Exception as e:
            logger.warning("呼叫前端確認關閉失敗：%s", e)
        return False  # 取消本次關閉，等使用者於彈窗確認

    def stop_all_services_sync(self) -> None:
        """同步停止全部背景服務並等待完成（關閉流程用，期間前端顯示轉圈）。"""
        try:
            self.stop_worker()
        except Exception as e:
            logger.error("停止 STT worker 失敗：%s", e)
        try:
            self.stop_stt_container()
        except Exception as e:
            logger.error("停止 whisper 容器失敗：%s", e)
        # 一次停掉所有 compose 服務（llm-service / openclaw），阻塞至完成
        try:
            with self._compose_lock:
                self._run_compose(["stop"], timeout=180)
        except Exception as e:
            logger.error("停止 compose 服務失敗：%s", e)
        self.__class__._services_cache = None

    def shutdown_app(self) -> dict:
        """前端確認關閉後呼叫：停掉所有服務，完成後關閉主視窗。"""
        self.__class__._shutting_down = True
        logger.info("關閉中：停止所有背景服務…")
        try:
            self.stop_all_services_sync()
        finally:
            logger.info("服務已停止，關閉視窗")
            if self._window:
                try:
                    self._window.destroy()
                except Exception as e:
                    logger.error("關閉視窗失敗：%s", e)
        return _ok({"message": "已關閉"})
