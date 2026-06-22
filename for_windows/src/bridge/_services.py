"""服務管理（純 Windows 版）— 大幅簡化。

原系統有四個背景服務（whisper 容器 / stt_worker / llm-service / openclaw）。純 Windows
版把轉譯與整理合併進「單一處理 worker」（worker_main.py 的雙階段 pipeline），不再有
Docker / compose。對前端呈現兩個項目：

    worker   處理服務（轉譯 + 整理），可手動啟停。
    claude   Claude CLI 相依（雙偵測），只回報偵測狀態、不由本程式啟停（使用者自行安裝）。

關閉生命週期（確認彈窗 → 停 worker → destroy）沿用原設計，避免 GUI 主執行緒死鎖。
"""

import time
import logging
import threading

import src.claude_cli as claude_cli
from src.bridge._helpers import _ok, _err, ErrorType
from src.bridge._worker import _worker_running

logger = logging.getLogger(__name__)

WORKER_KEY = "worker"

SERVICE_META = {
    WORKER_KEY: {"name": "處理服務", "desc": "轉譯 + 會議紀錄整理（背景 pipeline）"},
}


class ServicesMixin:
    """處理 worker 的啟停 + Claude 偵測狀態，與關閉生命週期。"""

    _services_cache: tuple[float, dict] | None = None  # (時間, status dict)
    _shutting_down = False   # 關閉流程已確認 → 放行視窗關閉
    _starting: set = set()   # 正在啟動中的服務 key（讓前端顯示「啟動中」）

    def _claude_token(self) -> str:
        return self._load_config().get("api_keys", {}).get("claude", "").strip()

    # ── 狀態組裝 ──

    def _worker_card(self) -> dict:
        running = _worker_running()
        status = "running" if running else ("starting" if WORKER_KEY in self._starting else "stopped")
        return {
            "key": WORKER_KEY,
            "name": SERVICE_META[WORKER_KEY]["name"],
            "description": SERVICE_META[WORKER_KEY]["desc"],
            "running": running,
            "status": status,
            "message": "",
        }

    def _claude_card(self) -> dict:
        info = claude_cli.detect()
        running = info["available"]
        if running:
            status = "running"
            mode = "（Windows 原生）" if info["mode"] == "windows" else "（WSL）"
            message = f"已偵測{mode}"
        else:
            status = "absent"
            message = "未安裝，請於 Windows 或 WSL 安裝後重新偵測"
        return {
            "key": "claude",
            "name": "Claude CLI",
            "description": "會議紀錄整理引擎（claude -p，雙偵測）",
            "running": running,
            "status": status,
            "message": message,
            "mode": info["mode"],
            "detectable_only": True,   # 前端據此隱藏啟停鈕、改顯示「重新偵測 / 前往安裝」
        }

    # ── Public API ──

    def get_services_status(self) -> dict:
        """處理服務 + Claude 偵測狀態（含 2.5s 快取）。"""
        now = time.time()
        if self._services_cache and now - self._services_cache[0] < 2.5:
            return _ok(self._services_cache[1])
        out = {WORKER_KEY: self._worker_card(), "claude": self._claude_card()}
        self.__class__._services_cache = (now, out)
        return _ok(out)

    def get_system_health(self) -> dict:
        """單一健康指示：給側邊欄 / 服務頁 / 儀表板用。

        level: ready（處理服務在跑）| starting（啟動中）| attention（未啟動）
        Claude 未就緒不擋「就緒」（錄音與轉譯仍可），但會在訊息附註整理不可用。
        """
        worker_up = _worker_running()
        claude_ok = claude_cli.available()
        running = (1 if worker_up else 0) + (1 if claude_ok else 0)
        total = 2

        if worker_up:
            level = "ready"
            message = "系統就緒，可開始錄音"
            if not claude_ok:
                message = "可錄音與轉譯；未偵測到 Claude CLI，逐字稿不會自動整理"
        elif WORKER_KEY in self._starting:
            level, message = "starting", "處理服務啟動中…"
        else:
            level, message = "attention", "處理服務未啟動"

        return _ok({"level": level, "message": message, "running": running, "total": total})

    def start_service(self, key: str) -> dict:
        if key == "claude":
            return _err(ErrorType.VALIDATION, "Claude CLI 由使用者自行安裝，無法由本程式啟動")
        if key != WORKER_KEY:
            return _err(ErrorType.VALIDATION, f"未知服務：{key}")

        self.__class__._starting.add(key)
        self.__class__._services_cache = None

        def _run():
            try:
                self.start_worker_detached()
            except Exception as e:
                logger.error("啟動處理服務失敗：%s", e)
            finally:
                self.__class__._starting.discard(key)
                self.__class__._services_cache = None

        threading.Thread(target=_run, name="svc-start-worker", daemon=True).start()
        return _ok({"message": f"{SERVICE_META[WORKER_KEY]['name']}啟動中…"})

    def stop_service(self, key: str) -> dict:
        if key == "claude":
            return _err(ErrorType.VALIDATION, "Claude CLI 非由本程式管理，無法停止")
        if key != WORKER_KEY:
            return _err(ErrorType.VALIDATION, f"未知服務：{key}")
        self.__class__._services_cache = None
        ok = self.stop_worker()
        return _ok({"message": "處理服務已停止"}) if ok else _err(ErrorType.INTERNAL, "停止處理服務失敗")

    def start_all_services(self) -> dict:
        self.start_all_services_async()
        return _ok({"message": "已送出啟動指令"})

    def stop_all_services(self) -> dict:
        return self.stop_service(WORKER_KEY)

    def start_all_services_async(self) -> None:
        """app 啟動時呼叫：拉起處理 worker（先判斷是否已在執行）。"""
        threading.Thread(target=lambda: self.start_service(WORKER_KEY),
                         name="services-startup", daemon=True).start()

    # ── 關閉生命週期 ──

    def on_window_closing(self, *args) -> bool:
        """pywebview 視窗關閉事件處理（*args 容忍不同版本的呼叫慣例）。

        首次關閉：取消這次關閉（回傳 False），改叫前端跳「確認關閉」彈窗。
        確認後 shutdown_app() 會設 _shutting_down 並停服務、再 destroy →
        destroy 觸發的這次事件就放行（回傳 True）。
        """
        if self.__class__._shutting_down:
            return True
        # 絕不可在 closing 事件（GUI 主執行緒）裡同步呼叫 evaluate_js —— 會與需要主執行緒
        # 跑 JS 的後端互等死鎖。改丟背景執行緒呼叫。
        def _ask_frontend():
            try:
                if self._window:
                    self._window.evaluate_js("window.__confirmShutdown && window.__confirmShutdown()")
            except Exception as e:
                logger.warning("呼叫前端確認關閉失敗：%s", e)
        threading.Thread(target=_ask_frontend, name="ask-shutdown", daemon=True).start()
        return False

    def stop_all_services_sync(self) -> None:
        """同步停止背景處理 worker 並等待完成（關閉流程用）。"""
        try:
            self.stop_worker()
        except Exception as e:
            logger.error("停止處理 worker 失敗：%s", e)
        self.__class__._services_cache = None

    def shutdown_app(self) -> dict:
        """前端確認關閉後呼叫：停服務 + 關視窗，但「立即返回」避免 GUI 主執行緒死鎖。"""
        if self.__class__._shutting_down:
            return _ok({"message": "關閉中"})
        self.__class__._shutting_down = True

        def _stop_and_close():
            logger.info("關閉中：停止背景處理服務…")
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
