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
COMPOSE_SVC = {"llm_service": "llm-service"}   # openclaw 已停用（見下）

# 啟動順序（whisper / worker 先就緒，再起下游 compose）
# openclaw 已停用：整理改由 app 的「轉會議紀錄」按鈕（generate_result）觸發，不需自動整理容器。
SERVICE_ORDER = ("whisper", "stt_worker", "llm_service")   # , "openclaw"

# 「系統就緒」只看核心服務（錄音→逐字稿）。LLM／openclaw 需使用者填 Claude token
# 才會啟動，未填屬正常狀態，不應讓整體健康卡在「未就緒」，故不納入核心。
CORE_SERVICES = ("whisper", "stt_worker")

SERVICE_META = {
    "whisper":     {"name": "Whisper 語音引擎", "desc": "本地語音轉文字容器（port 9000）"},
    "stt_worker":  {"name": "STT Worker",       "desc": "監看錄音資料夾並送轉譯"},
    "llm_service": {"name": "LLM 服務",          "desc": "OpenAI 相容介面，後端 claude -p"},
    # "openclaw":    {"name": "OpenClaw 整理",     "desc": "逐字稿 → 會議紀錄 Markdown"},   # 已停用
}

# native 模式：沿用相同四個 key（前端服務頁共用），但語意改為本機原生引擎。
# whisper/llm_service/openclaw 為「偵測/衍生」狀態，無獨立程序可啟停，唯一可控的是 worker。
NATIVE_SERVICE_META = {
    "whisper":     {"name": "轉譯引擎",        "desc": "本機 faster-whisper（in-process，無需 Docker）"},
    "stt_worker":  {"name": "處理服務",        "desc": "監看錄音 → 轉譯 → 整理（背景 pipeline）"},
    "llm_service": {"name": "Claude 整理引擎",  "desc": "claude -p（雙偵測：原生優先、WSL 後備）"},
    # "openclaw":    {"name": "自動整理",        "desc": "逐字稿 → 會議紀錄（內建於處理服務）"},   # 已停用
}

_CREATE_NO_WINDOW = 0x08000000  # Windows：不閃主控台視窗


class ServicesMixin:
    """四個背景服務的統一啟停與狀態查詢。"""

    _compose_lock = threading.Lock()
    _services_cache: tuple[float, dict] | None = None  # (時間, status dict)
    _wsl_docker_cache: tuple[float, bool, str] | None = None  # (時間, ok, message)
    _shutting_down = False  # 關閉流程已確認 → 放行視窗關閉
    _starting: set = set()  # 正在啟動中的服務 key（讓前端顯示「啟動中」）

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

    def _wsl_docker_ok(self) -> tuple[bool, str]:
        """Windows：WSL 內的 docker 是否連得到引擎（compose 服務走 WSL，需此就緒）。

        Docker Desktop 引擎在 Windows 端可連（whisper 直接用 docker.exe），但若沒對
        當前 WSL distro 開「WSL 整合」，`wsl docker` 會連不到引擎（/var/run/docker.sock
        不存在），導致 compose 服務（llm-service / openclaw）起不來、錯誤只埋在日誌裡。
        提前探測，給清楚可行動的回饋。含 10 秒快取，避免每次啟動都跑 wsl。
        """
        now = time.time()
        cached = self.__class__._wsl_docker_cache
        if cached and now - cached[0] < 10.0:
            return cached[1], cached[2]

        hint = ("WSL 內連不到 Docker 引擎：請開啟 Docker Desktop → Settings → Resources → "
                "WSL Integration，啟用目前的 WSL 發行版後 Apply & Restart，再重試。")
        ok, msg = False, ""
        try:
            r = subprocess.run(
                ["wsl", "-e", "bash", "-lc", "docker info --format '{{.ServerVersion}}'"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, creationflags=_CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and (r.stdout or "").strip():
                ok = True
            else:
                msg = hint
                logger.warning("WSL 內 docker info 失敗：%s", (r.stderr or "").strip()[:200])
        except Exception as e:
            msg = hint
            logger.warning("WSL docker 預檢執行失敗：%s", e)

        self.__class__._wsl_docker_cache = (now, ok, msg)
        return ok, msg

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
        if "whisper" in self._starting:
            return self._svc("whisper", running=False, status="starting")
        return self._svc("whisper", running=False,
                         status="stopped" if cs == "stopped" else "absent")

    # ── Public API ──

    def get_services_status(self) -> dict:
        """四個服務的狀態（含 2.5s 快取，避免輪詢頻繁打 docker / WSL）。"""
        now = time.time()
        if self._services_cache and now - self._services_cache[0] < 2.5:
            return _ok(self._services_cache[1])

        if self._pipeline_mode() == "native":
            out = self._services_status_native()
            self.__class__._services_cache = (now, out)
            return _ok(out)

        docker_state, docker_msg = self._docker_state()
        docker_ok = docker_state == "ok"

        worker_up = _worker_running()
        worker_status = "running" if worker_up else (
            "starting" if "stt_worker" in self._starting else "stopped")
        out: dict = {
            "whisper": self._whisper_status(docker_ok, docker_msg),
            "stt_worker": self._svc("stt_worker", running=worker_up, status=worker_status),
        }

        compose = self._compose_status_all() if docker_ok else {}
        for key, svc in COMPOSE_SVC.items():
            if not docker_ok:
                out[key] = self._svc(key, running=False, status="error", message=docker_msg)
            else:
                running = compose.get(svc, False)
                status = "running" if running else (
                    "starting" if key in self._starting else "stopped")
                out[key] = self._svc(key, running=running, status=status)

        self.__class__._services_cache = (now, out)
        return _ok(out)

    def _services_status_native(self) -> dict:
        """native 模式服務狀態（前端服務頁共用 key）。

        只有 stt_worker 是可啟停的真實程序；whisper（faster-whisper 引擎）、
        llm_service（claude -p）為偵測型引擎，標 detectable_only（前端只給「重新偵測」）。
        不含 openclaw —— 那是 Docker 版的自動整理容器，native + 手動模式用不到
        （整理改由「轉會議紀錄」按鈕直接呼叫 claude）。
        """
        try:
            from src.bridge._docker import _native_engine_ready
            engine_ok = _native_engine_ready()
        except Exception:
            engine_ok = False
        try:
            import src.claude_cli as claude_cli
            claude_ok = claude_cli.available()
        except Exception:
            claude_ok = False
        worker_up = _worker_running()
        starting = "stt_worker" in self._starting

        def _card(key, running, status, message=""):
            m = NATIVE_SERVICE_META[key]
            return {"key": key, "name": m["name"], "description": m["desc"],
                    "running": running, "status": status, "message": message,
                    "detectable_only": key != "stt_worker"}

        return {
            "whisper": _card("whisper", engine_ok, "running" if engine_ok else "absent",
                             "" if engine_ok else "需安裝 faster-whisper"),
            "stt_worker": _card("stt_worker", worker_up,
                                "running" if worker_up else ("starting" if starting else "stopped")),
            "llm_service": _card("llm_service", claude_ok, "running" if claude_ok else "absent",
                                 "" if claude_ok else "未偵測到 Claude CLI"),
        }

    def redetect_services(self) -> dict:
        """重新偵測本機引擎（faster-whisper / claude CLI），清快取後回傳最新服務狀態。

        給前端偵測型引擎卡片的「重新偵測」鈕用（裝好 faster-whisper / claude 後即時反映）。
        """
        try:
            import src.claude_cli as claude_cli
            claude_cli.detect(force=True)
        except Exception:
            pass
        self.__class__._services_cache = None
        return self.get_services_status()

    def get_pipeline_info(self) -> dict:
        """目前管線模式：mode（native/docker）+ auto（全自動 / 逐檔手動）。供錄音頁流程圖顯示。"""
        return _ok({"mode": self._pipeline_mode(), "auto": self._auto_pipeline()})

    def set_pipeline_auto(self, auto) -> dict:
        """切換全自動 / 逐檔手動。改 config 後重啟處理 worker 讓 STT_AUTO 生效。"""
        auto = bool(auto)
        cfg = self._load_config()
        cfg.setdefault("pipeline", {})["auto"] = auto
        self._save_config(cfg)
        try:
            if _worker_running():
                self.stop_worker()
                self.start_worker_detached()
        except Exception as e:
            logger.warning("切換自動模式後重啟 worker 失敗：%s", e)
        return _ok({"auto": auto,
                    "message": "自動處理已開啟（錄完自動轉譯並整理）" if auto
                               else "已切換為逐檔手動（自行按轉逐字稿 / 轉會議紀錄）"})

    def get_log(self, source: str = "worker", lines: int = 300) -> dict:
        """讀取某來源的最近日誌，給「日誌」分頁用。

        來源：
            worker      → 處理 worker 的 log 檔（detached 程序無主控台，寫在 _data_root/worker.log）
            whisper     → docker logs（whisper 容器；native 模式無此來源）
            llm_service → docker compose logs llm-service
            openclaw    → docker compose logs openclaw
        回傳 {source, text, available, message}。
        """
        n = max(20, min(int(lines or 300), 2000))
        try:
            if source == "worker":
                path = getattr(self, "_data_root", self._project_root) / "worker.log"
                if not path.exists():
                    return _ok({"source": source, "text": "", "available": False,
                                "message": f"尚無日誌（worker 尚未產生 {path.name}）"})
                data = path.read_text(encoding="utf-8", errors="replace").splitlines()
                return _ok({"source": source, "text": "\n".join(data[-n:]), "available": True})

            # 以下為容器日誌；native 模式無容器
            if self._pipeline_mode() == "native":
                return _ok({"source": source, "text": "", "available": False,
                            "message": "native（免 Docker）模式無容器日誌，僅有處理 worker 日誌"})

            if source == "whisper":
                from src.bridge._docker import _run_docker, STT_CONTAINER
                r = _run_docker(["logs", "--tail", str(n), STT_CONTAINER], timeout=20)
                text = ((r.stdout or "") + (r.stderr or "")).strip()
                return _ok({"source": source, "text": text, "available": True})

            if source in COMPOSE_SVC:
                r = self._run_compose(["logs", "--tail", str(n), "--no-color", COMPOSE_SVC[source]],
                                      timeout=40)
                text = ((r.stdout or "") + (r.stderr or "")).strip()
                return _ok({"source": source, "text": text, "available": True})

            return _err(ErrorType.VALIDATION, f"未知的日誌來源：{source}")
        except Exception as e:
            return _err(ErrorType.INTERNAL, f"讀取日誌失敗：{e}")

    def get_system_health(self) -> dict:
        """單一健康指示：把四個服務濃縮成一句白話狀態，給側邊欄 / 服務頁用。

        level: ready（全部就緒）| starting（啟動中）| attention（需處理）
        """
        if self._pipeline_mode() == "native":
            worker_up = _worker_running()
            try:
                import src.claude_cli as claude_cli
                claude_ok = claude_cli.available()
            except Exception:
                claude_ok = False
            running = (1 if worker_up else 0) + (1 if claude_ok else 0)
            if worker_up:
                level = "ready"
                message = ("系統就緒，可開始錄音" if claude_ok
                           else "可錄音與轉譯；未偵測到 Claude CLI，逐字稿不會自動整理")
            elif "stt_worker" in self._starting:
                level, message = "starting", "處理服務啟動中…"
            else:
                level, message = "attention", "處理服務未啟動"
            return _ok({"level": level, "message": message, "running": running, "total": 2})

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
        - native 模式：全程本機原生，無 Docker/WSL 前置依賴。
        """
        if self._pipeline_mode() == "native":
            return True, ""
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
            # Windows：compose 服務在 WSL 內跑，需 WSL 內 docker 連得到引擎
            # （Docker Desktop 的 WSL 整合）。whisper 走 docker.exe 不受此影響，故
            # 只在這兩個 compose 服務的啟動前把關，避免 compose up 深處才失敗。
            if sys.platform == "win32":
                wd_ok, wd_msg = self._wsl_docker_ok()
                if not wd_ok:
                    return False, wd_msg
            # LLM 需 Claude 訂閱 token；openclaw 依賴 LLM，故同樣需要
            if not self._claude_token():
                return False, "請先在設定填入 Claude 訂閱 Token（claude setup-token）才能啟動 LLM／OpenClaw"

        return True, ""

    def start_service(self, key: str) -> dict:
        if key not in SERVICE_META:
            return _err(ErrorType.VALIDATION, f"未知服務：{key}")

        # native：唯一可控的是處理 worker；其餘為本機引擎的偵測/衍生狀態
        if self._pipeline_mode() == "native" and key != "stt_worker":
            return _ok({"message": f"{NATIVE_SERVICE_META[key]['name']}為本機引擎，無需啟動"})

        ok, msg = self._service_deps_ok(key)
        if not ok:
            return _err(ErrorType.DOCKER_UNAVAILABLE, msg)

        # 標記「啟動中」→ 前端立即顯示啟動中、停用啟動鈕；背景啟動完成才清除
        self.__class__._starting.add(key)
        self.__class__._services_cache = None

        if key == "whisper":
            target, args = self.ensure_stt_container, ()
        elif key == "stt_worker":
            target, args = self.start_worker_detached, ()
        else:
            target, args = self._compose_up, (COMPOSE_SVC[key],)

        def _run():
            try:
                target(*args)
            except Exception as e:
                logger.error("啟動 %s 失敗：%s", key, e)
            finally:
                self.__class__._starting.discard(key)
                self.__class__._services_cache = None

        threading.Thread(target=_run, name=f"svc-start-{key}", daemon=True).start()
        return _ok({"message": f"{SERVICE_META[key]['name']} 啟動中…"})

    def stop_service(self, key: str) -> dict:
        if key not in SERVICE_META:
            return _err(ErrorType.VALIDATION, f"未知服務：{key}")
        self.__class__._services_cache = None

        if self._pipeline_mode() == "native" and key != "stt_worker":
            return _ok({"message": f"{NATIVE_SERVICE_META[key]['name']}為本機引擎，無需停止"})

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
            # native 只需拉處理 worker；docker 模式起容器服務。手動模式不起 openclaw
            # （自動整理器）——整理改由 app 的「轉會議紀錄」按鈕（generate_result）觸發。
            if self._pipeline_mode() == "native":
                keys = ("stt_worker",)
            elif self._auto_pipeline():
                keys = SERVICE_ORDER
            else:
                keys = tuple(k for k in SERVICE_ORDER if k != "openclaw")
            for key in keys:
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
        # 重要：絕不可在這個 closing 事件（GUI 主執行緒）裡「同步」呼叫 evaluate_js——
        # EdgeChromium 後端的 evaluate_js 需要主執行緒跑 JS，而主執行緒正卡在這個
        # 事件處理裡 → 互等死鎖（整個 UI 凍結、確認彈窗都出不來）。改丟背景執行緒呼叫。
        def _ask_frontend():
            try:
                if self._window:
                    self._window.evaluate_js("window.__confirmShutdown && window.__confirmShutdown()")
            except Exception as e:
                logger.warning("呼叫前端確認關閉失敗：%s", e)
        threading.Thread(target=_ask_frontend, name="ask-shutdown", daemon=True).start()
        return False  # 取消本次關閉，等使用者於彈窗確認

    def stop_all_services_sync(self) -> None:
        """同步停止全部背景服務並等待完成（關閉流程用，期間前端顯示轉圈）。"""
        if self._pipeline_mode() == "native":
            # native 只有處理 worker 一個程序，無容器 / compose 可停
            try:
                self.stop_worker()
            except Exception as e:
                logger.error("停止處理 worker 失敗：%s", e)
            self.__class__._services_cache = None
            return
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
        """前端確認關閉後呼叫：停服務 + 關視窗，但「立即返回」。

        關鍵：不可在這個 js_api 呼叫裡同步做完再 destroy() —— 那會讓 GUI 主執行緒
        卡在等這個呼叫返回，而 destroy() 又需要主執行緒處理 → 死鎖（停服務與關窗都卡住）。
        先 return 讓 JS 的 await 解決、主執行緒解放，背景執行緒再停服務、再 destroy。
        """
        if self.__class__._shutting_down:
            return _ok({"message": "關閉中"})
        self.__class__._shutting_down = True

        def _stop_and_close():
            logger.info("關閉中：停止所有背景服務…")
            try:
                self.stop_all_services_sync()
            except Exception as e:
                logger.error("停止服務時發生例外：%s", e)
            logger.info("服務已停止，關閉視窗")
            try:
                if self._window:
                    self._window.destroy()
            except Exception as e:
                logger.error("關閉視窗失敗：%s", e)

        threading.Thread(target=_stop_and_close, name="app-shutdown", daemon=True).start()
        return _ok({"message": "關閉中"})
