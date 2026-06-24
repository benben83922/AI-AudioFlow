"""STT worker 管理 — 隨 app 啟動，但以 detached 程序執行，關閉 app 後仍持續運作。

worker 跑在 Windows（讀 D:\\record 原生），逐字稿寫到 WSL 原生資料夾（供 openclaw 掛載讀取）。
靠 worker 自身的 localhost port 單例鎖去重：app 每次啟動先偵測，沒在跑才拉起。
"""

import os
import re
import sys
import signal
import logging
import subprocess
from pathlib import Path

from src import stt_lock
from src.bridge._helpers import _ok
from src.bridge._platform import detect_platform, WINDOWS, WSL

logger = logging.getLogger(__name__)

WORKER_LOCK_PORT = stt_lock.DEFAULT_LOCK_PORT   # 單例鎖 base port（worker 可能漂移）
DEFAULT_WHISPER_URL = "http://localhost:9000"
DEFAULT_LANGUAGE = "zh"
_CREATE_NO_WINDOW = 0x08000000    # Windows：不閃主控台視窗


def _is_frozen() -> bool:
    """是否為打包後（Nuitka / PyInstaller）執行；決定如何重新拉起 worker。"""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _onefile_binary() -> str:
    """打包後「自己」的執行檔絕對路徑，用來重新拉起 worker。

    Nuitka onefile 下 `sys.executable` 在 Linux 會指向解壓暫存夾裡並不存在的
    `python3`（Windows 才剛好指向 exe 本體），直接拿來 spawn 會噴
    `[Errno 2] No such file or directory: /tmp/onefile_xxx/python3`。
    Nuitka 會把 onefile 執行檔的絕對路徑放在環境變數 NUITKA_ONEFILE_BINARY，
    優先採用；退而求其次才用 argv[0]（轉絕對路徑，因 worker 會換 cwd）。
    """
    return (
        os.environ.get("NUITKA_ONEFILE_BINARY")
        or (sys.argv and os.path.abspath(sys.argv[0]))
        or sys.executable
    )


def _worker_running(port: "int | None" = None, pid_file: "Path | None" = None) -> bool:
    """worker 是否在跑：靠單例鎖 port 上的握手字串辨識（見 stt_lock）。

    指定 port → 只探測該 port；否則掃描漂移範圍 [base, base+span)，靠握手認出
    「我們的 worker」。握手辨識可避免「陌生程式剛好佔同一 port」被誤判成 worker
    在跑（舊版只查 port 是否監聽，會誤報）。直接 socket 連線跨平台一致，不需
    powershell（app 與 worker 在實際部署中同機,localhost 連得到）。
    """
    if port is not None:
        return stt_lock.probe(port)
    return stt_lock.is_running(WORKER_LOCK_PORT)


def _pid_on_port(port: int) -> int | None:
    """找出占用 lock port 的程序 PID。
    Windows 原生用 netstat；WSL2 用 powershell.exe；純 Linux 用 ss。"""
    try:
        plat = detect_platform()
        if plat == WINDOWS:
            r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=10, creationflags=_CREATE_NO_WINDOW)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                        and parts[1].endswith(f":{port}"):
                    return int(parts[4])
        elif plat == WSL:
            # WSL2：透過 powershell.exe 查 Windows 端的 TCP 監聽程序
            r = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-Command",
                    f"(Get-NetTCPConnection -LocalPort {port} -State Listen"
                    f" -ErrorAction SilentlyContinue).OwningProcess",
                ],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            pid_str = r.stdout.strip()
            if pid_str.isdigit():
                return int(pid_str)
        else:
            return _linux_pid_on_port(port)
    except Exception as e:
        logger.warning("查詢 port %d 的 PID 失敗：%s", port, e)
    return None


def _linux_pid_on_port(port: int) -> int | None:
    """純 Linux：用 ss 找出監聽該 port 的本機 PID。"""
    try:
        r = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
    except Exception as e:
        logger.warning("ss 查詢 port %d 失敗：%s", port, e)
        return None
    for line in r.stdout.splitlines():
        # 本機位址欄含 :<port>，且該行帶 users:(("...",pid=NNN,...))
        if re.search(rf"[:.]{port}\s", line):
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def _kill_pid(pid: int) -> bool:
    """終止指定 PID。Windows 原生用 taskkill；WSL2 用 taskkill.exe；純 Linux 用 SIGTERM。"""
    try:
        plat = detect_platform()
        if plat == WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=10, creationflags=_CREATE_NO_WINDOW)
        elif plat == WSL:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/F"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=10)
        else:
            # 純 Linux：SIGTERM 讓 worker 走正常收尾（它有註冊 SIGTERM 處理）
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception as e:
        logger.error("終止 PID %s 失敗：%s", pid, e)
        return False


class WorkerMixin:
    """STT worker 的 detached 啟動與狀態查詢。"""

    def _worker_env(self) -> dict:
        config = self._load_config()
        storage = config.get("storage", {})

        watch_dir = storage.get("local_path", "").strip() or str(self._recordings_dir)
        # 逐字稿輸出資料夾：吃 _base 的解析（未設定時依裝置自動偵測 WSL stt-outbox）
        outbox = self._transcripts_dir_path()

        env = dict(os.environ)
        env["STT_WATCH_DIR"] = watch_dir
        if outbox is not None:
            env["STT_OUTBOX_DIR"] = str(outbox)
        env["WHISPER_URL"] = storage.get("whisper_url", "").strip() or DEFAULT_WHISPER_URL
        env["STT_LANGUAGE"] = DEFAULT_LANGUAGE
        env["STT_LOCK_PORT"] = str(WORKER_LOCK_PORT)
        # CPU 轉譯長錄音可能超過預設 600s，放寬逾時避免長檔轉到一半被砍（可被環境變數覆寫）
        env.setdefault("STT_REQUEST_TIMEOUT", "1800")
        # detached 程序沒有主控台，logging 導向檔案方便查看（打包後寫在 exe 同層）
        env["STT_LOG_FILE"] = str(getattr(self, "_data_root", self._project_root) / "worker.log")

        # 自動 vs 手動：手動模式下 worker 只在收到「轉逐字稿」請求標記時才轉譯，整理一律
        # 留給 app 按鈕（generate_result）。
        env["STT_AUTO"] = "1" if self._auto_pipeline() else "0"

        # 後端模式：docker（HTTP→whisper 容器，整理交給 openclaw）vs native（本機
        # faster-whisper + claude -p，worker 內一條龍做完轉譯與整理，不需 Docker）。
        mode = self._pipeline_mode()
        env["STT_BACKEND"] = mode
        if mode == "native":
            results = self._results_dir_path()
            if results is not None:
                env["STT_RESULTS_DIR"] = str(results)
            stt = config.get("stt", {})
            env["WHISPER_MODEL"] = stt.get("model", "large-v3-turbo")
            env["WHISPER_DEVICE"] = stt.get("device", "cpu")
            env["WHISPER_COMPUTE_TYPE"] = stt.get("compute_type", "int8")
            # 模型快取（首次自動下載到這；亦可預先放好達成「執行時不下載」）
            env["WHISPER_DOWNLOAD_ROOT"] = str(
                getattr(self, "_data_root", self._project_root) / "models")
            token = config.get("api_keys", {}).get("claude", "").strip()
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token   # 否則靠使用者於 host/WSL 已登入
            env["CLAUDE_MODEL"] = "opus"
            env["SUMMARY_PROMPT"] = self._summary_prompt()   # 自訂會議紀錄 prompt（空＝預設）
        return env

    def _worker_launch_cmd(self) -> list[str]:
        """組出拉起 worker 的指令（內嵌 worker 模式）。

        打包後：主 exe 重新拉起自己 → `app.exe --worker`。
        原始碼：以模組方式跑 → `python -m src.main --worker`。
        兩者最終都會執行 src/worker_main.py:main()。
        """
        if _is_frozen():
            return [_onefile_binary(), "--worker"]
        return [sys.executable, "-m", "src.main", "--worker"]

    def start_worker_detached(self) -> None:
        """若 worker 尚未在跑，以 detached 程序拉起（關閉 app 後仍持續）。"""
        if _worker_running():
            logger.info("STT worker 已在執行，略過啟動")
            return

        kwargs: dict = {
            "cwd": str(self._project_root),  # 供原始碼模式解析 `-m src.main`
            "env": self._worker_env(),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：脫離父程序、不隨 app 關閉而死
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(self._worker_launch_cmd(), **kwargs)
            # 寫 pidfile 供 stop_worker 終止（detached 程序跨 app 重啟仍可被收掉）
            try:
                self._worker_pid_file().write_text(str(proc.pid), encoding="utf-8")
            except Exception as e:
                logger.warning("寫入 worker pidfile 失敗：%s", e)
            logger.info("STT worker 已以 detached 程序啟動 (pid=%s)", proc.pid)
        except Exception as e:
            logger.error("啟動 STT worker 失敗：%s", e)

    def stop_worker(self) -> bool:
        """停止 STT worker：優先用 pidfile，失敗則回退用 lock port 反查 PID。"""
        if not _worker_running():
            self._worker_pid_file().unlink(missing_ok=True)
            return True

        # 回退反查 PID：用 worker 實際監聽的 port（可能已漂移過），找不到就退回 base
        actual_port = stt_lock.running_port(WORKER_LOCK_PORT) or WORKER_LOCK_PORT
        pid = self._read_worker_pid() or _pid_on_port(actual_port)
        if pid is None:
            logger.warning("找不到 STT worker PID，無法停止")
            return False

        if _kill_pid(pid):
            self._worker_pid_file().unlink(missing_ok=True)
            logger.info("STT worker 已停止 (pid=%s)", pid)
            return True
        return False

    def _worker_pid_file(self) -> Path:
        return self._project_root / "stt-worker" / "worker.pid"

    def _read_worker_pid(self) -> int | None:
        try:
            return int(self._worker_pid_file().read_text(encoding="utf-8").strip())
        except Exception:
            return None

    # ── Public API ──

    def get_worker_status(self) -> dict:
        return _ok({"running": _worker_running()})
