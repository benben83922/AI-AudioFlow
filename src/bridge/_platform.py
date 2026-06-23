"""執行環境偵測 — 區分原生 Windows / WSL2 / 純 Linux。

`sys.platform` 只能分出 win32 與 linux，無法區分「WSL2」與「純 Linux」：兩者
`sys.platform` 都是 'linux'，但 WSL2 能透過 .exe interop 反呼叫 Windows 主機
（powershell.exe / taskkill.exe），純 Linux 不能。worker 管理與錄音這兩塊正好
依賴此差異，故集中在這裡判斷，全程式共用一處真相，避免 win32/else 兩分把
WSL 與純 Linux 混為一談。
"""

from __future__ import annotations

import os
import sys
import shutil
import functools
from pathlib import Path

WINDOWS = "windows"
WSL = "wsl"
LINUX = "linux"


def _is_wsl() -> bool:
    """WSL 指紋：WSL 專屬環境變數，或核心版本字串含 microsoft。"""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        rel = Path("/proc/sys/kernel/osrelease").read_text(
            encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "microsoft" in rel or "wsl" in rel


@functools.lru_cache(maxsize=1)
def detect_platform() -> str:
    """回傳 'windows' | 'wsl' | 'linux'（結果快取，執行期間環境不會改變）。"""
    if sys.platform == "win32":
        return WINDOWS
    if sys.platform.startswith("linux"):
        return WSL if _is_wsl() else LINUX
    # 其餘 Unix（macOS 等）目前一律走純 Linux 路徑
    return LINUX


def is_windows() -> bool:
    return detect_platform() == WINDOWS


def is_wsl() -> bool:
    return detect_platform() == WSL


def is_linux() -> bool:
    return detect_platform() == LINUX


def uses_windows_host() -> bool:
    """是否能/應透過 Windows 主機工具（powershell.exe / taskkill.exe）操作。

    原生 Windows 與 WSL2 皆可（WSL2 走 .exe interop）；純 Linux 不行。
    """
    return detect_platform() in (WINDOWS, WSL)


def windows_interop_ok() -> bool:
    """WSL 的 .exe interop 是否實際可用（少數環境會關閉）。"""
    return shutil.which("powershell.exe") is not None
