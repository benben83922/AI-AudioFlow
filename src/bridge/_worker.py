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

WORKER_LOCK_PORT = 47654          # 與 worker_main.py 的 STT_LOCK_PORT 預設一致
DEFAULT_WHISPER_URL = "http://localhost:9000"
DEFAULT_LANGUAGE = "zh"
_CREATE_NO_WINDOW = 0x08000000    # Windows：不閃主控台視窗


def _is_frozen() -> bool:
    """是否為打包後（Nuitka / PyInstaller）執行；決定如何重新拉起 worker。"""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _worker_running(port: int = WORKER_LOCK_PORT, pid_file: "Path | None" = None) -> bool:
    """Windows 側 worker 的單例鎖 port 用 listen(1) 且從不 accept()，
    connect_ex 連一次就塞滿 backlog，導致後續全部被拒。
    改用 powershell.exe Get-NetTCPConnection 查詢監聽狀態（Windows / WSL2 皆適用）。
    """
    return _win_port_listening(port)


def _win_port_listening(port: int) -> bool:
    """用 powershell.exe 確認 Windows 端是否有程序監聽指定 port。
    在 Windows 原生與 WSL2 環境下皆可呼叫。
    """
    try:
        r = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                f"(Get-NetTCPConnection -LocalPort {port} -State Listen"
                f" -ErrorAction SilentlyContinue) -ne $null",
            ],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        return r.stdout.strip().upper() == "TRUE"
    except Exception:
        return False


def _pid_on_port(port: int) -> int | None:
    """找出占用 lock port 的 Windows PID。
    Windows 原生用 netstat；WSL2 用 powershell.exe Get-NetTCPConnection。"""
    try:
        if sys.platform == "win32":
            r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=10, creationflags=_CREATE_NO_WINDOW)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                        and parts[1].endswith(f":{port}"):
                    return int(parts[4])
        else:
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
    except Exception as e:
        logger.warning("查詢 port %d 的 PID 失敗：%s", port, e)
    return None


def _kill_pid(pid: int) -> bool:
    """終止指定 PID。Windows 原生用 taskkill；WSL2 用 taskkill.exe。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=10, creationflags=_CREATE_NO_WINDOW)
        else:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/F"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=10)
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
        return env

    def _worker_launch_cmd(self) -> list[str]:
        """組出拉起 worker 的指令（內嵌 worker 模式）。

        打包後：主 exe 重新拉起自己 → `app.exe --worker`。
        原始碼：以模組方式跑 → `python -m src.main --worker`。
        兩者最終都會執行 src/worker_main.py:main()。
        """
        if _is_frozen():
            return [sys.executable, "--worker"]
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
