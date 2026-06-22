"""處理 worker 管理（純 Windows 版）— 隨 app 啟動，detached 執行，關 app 後仍持續。

worker 在 Windows 原生執行：讀錄音資料夾 → faster-whisper 轉譯 → claude -p 整理，
全程不依賴 Docker / WSL。靠 worker 自身的 localhost port 單例鎖去重：app 每次啟動先
偵測，沒在跑才拉起。
"""

import os
import sys
import socket  # noqa: F401  (保留：未來若改用 socket 偵測)
import logging
import subprocess
from pathlib import Path

from src.bridge._helpers import _ok

logger = logging.getLogger(__name__)

WORKER_LOCK_PORT = 47654          # 與 worker_main.py 的 STT_LOCK_PORT 預設一致
DEFAULT_LANGUAGE = "zh"
_CREATE_NO_WINDOW = 0x08000000    # Windows：不閃主控台視窗


def _is_frozen() -> bool:
    """是否為打包後（Nuitka / PyInstaller）執行；決定如何重新拉起 worker。"""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _worker_running(port: int = WORKER_LOCK_PORT) -> bool:
    """worker 的單例鎖 port 用 listen(1) 且從不 accept()，connect 會塞滿 backlog，
    故改用 powershell.exe Get-NetTCPConnection 查詢監聽狀態（Windows / WSL2 皆適用）。"""
    return _win_port_listening(port)


def _win_port_listening(port: int) -> bool:
    """用 powershell.exe 確認 Windows 端是否有程序監聽指定 port。"""
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
    """找出占用 lock port 的 Windows PID。Windows 原生用 netstat；WSL2 用 powershell.exe。"""
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
    """處理 worker 的 detached 啟動與狀態查詢。"""

    def _worker_env(self) -> dict:
        config = self._load_config()
        storage = config.get("storage", {})
        stt = config.get("stt", {})

        env = dict(os.environ)
        env["AUDIOFLOW_DATA_ROOT"] = str(self._data_root)
        env["STT_WATCH_DIR"] = str(self._recordings_dir_path())
        env["STT_TRANSCRIPTS_DIR"] = str(self._transcripts_dir_path())
        env["STT_RESULTS_DIR"] = str(self._results_dir_path())
        # STT（faster-whisper）設定
        env["WHISPER_MODEL"] = stt.get("model", "").strip() or "large-v3-turbo"
        env["WHISPER_DEVICE"] = stt.get("device", "").strip() or "cpu"
        env["WHISPER_COMPUTE_TYPE"] = stt.get("compute_type", "").strip() or "int8"
        env["STT_LANGUAGE"] = stt.get("language", "").strip() or DEFAULT_LANGUAGE
        # 模型快取目錄：放工作目錄下 models/，模型只下載一次（可預先放好達成零下載）
        env["WHISPER_DOWNLOAD_ROOT"] = str(self._data_root / "models")
        # 整理（claude -p）設定
        token = config.get("api_keys", {}).get("claude", "").strip()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env["CLAUDE_MODEL"] = config.get("llm", {}).get("model", "").strip() or "opus"
        env.setdefault("CLAUDE_TIMEOUT", "600")
        # 守衛 / log
        env["STT_LOCK_PORT"] = str(WORKER_LOCK_PORT)
        env["STT_LOG_FILE"] = str(self._data_root / "worker.log")
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
            logger.info("處理 worker 已在執行，略過啟動")
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
            try:
                self._worker_pid_file().write_text(str(proc.pid), encoding="utf-8")
            except Exception as e:
                logger.warning("寫入 worker pidfile 失敗：%s", e)
            logger.info("處理 worker 已以 detached 程序啟動 (pid=%s)", proc.pid)
        except Exception as e:
            logger.error("啟動處理 worker 失敗：%s", e)

    def stop_worker(self) -> bool:
        """停止 worker：優先用 pidfile，失敗則回退用 lock port 反查 PID。"""
        if not _worker_running():
            self._worker_pid_file().unlink(missing_ok=True)
            return True

        pid = self._read_worker_pid() or _pid_on_port(WORKER_LOCK_PORT)
        if pid is None:
            logger.warning("找不到處理 worker PID，無法停止")
            return False

        if _kill_pid(pid):
            self._worker_pid_file().unlink(missing_ok=True)
            logger.info("處理 worker 已停止 (pid=%s)", pid)
            return True
        return False

    def _worker_pid_file(self) -> Path:
        return self._data_root / "worker.pid"

    def _read_worker_pid(self) -> int | None:
        try:
            return int(self._worker_pid_file().read_text(encoding="utf-8").strip())
        except Exception:
            return None

    # ── Public API ──

    def get_worker_status(self) -> dict:
        return _ok({"running": _worker_running()})
