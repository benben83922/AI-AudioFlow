"""STT worker 管理 — 隨 app 啟動，但以 detached 程序執行，關閉 app 後仍持續運作。

worker 跑在 Windows（讀 D:\\record 原生），逐字稿寫到 WSL 原生資料夾（供 openclaw 掛載讀取）。
靠 worker 自身的 localhost port 單例鎖去重：app 每次啟動先偵測，沒在跑才拉起。
"""

import os
import re
import sys
import signal
import socket
import logging
import subprocess
from pathlib import Path

from src.bridge._helpers import _ok

logger = logging.getLogger(__name__)

WORKER_LOCK_PORT = 47654          # 與 worker.py 的 STT_LOCK_PORT 預設一致
DEFAULT_WHISPER_URL = "http://localhost:9000"
DEFAULT_LANGUAGE = "zh"
_CREATE_NO_WINDOW = 0x08000000    # Windows：不閃主控台視窗


def _worker_running(port: int = WORKER_LOCK_PORT) -> bool:
    """嘗試連線單例鎖 port；連得上代表 worker 正在執行。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pid_on_port(port: int) -> int | None:
    """備援：當 pidfile 遺失（例如 worker 是前一個 app 實例拉起的）時，
    用系統工具找出占用 lock port 的 PID。"""
    try:
        if sys.platform == "win32":
            r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=10, creationflags=_CREATE_NO_WINDOW)
            for line in r.stdout.splitlines():
                parts = line.split()
                # Proto  Local            Foreign          State       PID
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                        and parts[1].endswith(f":{port}"):
                    return int(parts[4])
        else:
            r = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=10)
            for line in r.stdout.splitlines():
                if f":{port} " in line or line.rstrip().endswith(f":{port}"):
                    m = re.search(r"pid=(\d+)", line)
                    if m:
                        return int(m.group(1))
    except Exception as e:
        logger.warning("查詢 port %d 的 PID 失敗：%s", port, e)
    return None


def _kill_pid(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=10, creationflags=_CREATE_NO_WINDOW)
        else:
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
        outbox_dir = storage.get("transcripts_path", "").strip()

        env = dict(os.environ)
        env["STT_WATCH_DIR"] = watch_dir
        if outbox_dir:
            env["STT_OUTBOX_DIR"] = outbox_dir
        env["WHISPER_URL"] = storage.get("whisper_url", "").strip() or DEFAULT_WHISPER_URL
        env["STT_LANGUAGE"] = DEFAULT_LANGUAGE
        env["STT_LOCK_PORT"] = str(WORKER_LOCK_PORT)
        # detached 程序沒有主控台，logging 導向檔案方便查看
        env["STT_LOG_FILE"] = str(self._project_root / "stt-worker" / "worker.log")
        return env

    def start_worker_detached(self) -> None:
        """若 worker 尚未在跑，以 detached 程序拉起（關閉 app 後仍持續）。"""
        if _worker_running():
            logger.info("STT worker 已在執行，略過啟動")
            return

        worker_path = self._project_root / "stt-worker" / "worker.py"
        if not worker_path.exists():
            logger.error("找不到 worker：%s", worker_path)
            return

        kwargs: dict = {
            "cwd": str(worker_path.parent),
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
            proc = subprocess.Popen([sys.executable, str(worker_path)], **kwargs)
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

        pid = self._read_worker_pid() or _pid_on_port(WORKER_LOCK_PORT)
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
