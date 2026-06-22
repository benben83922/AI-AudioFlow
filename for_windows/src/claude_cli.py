"""Claude CLI 偵測與呼叫 — 雙偵測（Windows 原生優先，WSL 後備）。

純 Windows 版的「整理」階段不再經過 llm-service / openclaw，改由本模組直接呼叫
`claude -p`（訂閱模式）。完整 prompt（system + 指令 + 逐字稿）走 **stdin** 餵入，
避開跨界（Windows ↔ WSL）的引號與 CJK 編碼問題。

偵測順序：
    1. Windows 原生（PATH 或常見安裝位置 + `claude --version` 確認可執行）。
    2. WSL（`wsl -e bash -lc "command -v claude"`）。
皆無 → 不可用；由前端提示安裝。整理能力以此為閘門，但**不影響錄音與轉譯**。

認證：訂閱 token（CLAUDE_CODE_OAUTH_TOKEN，`claude setup-token` 取得）。
    - Windows 模式：直接帶入子程序環境。
    - WSL 模式：透過 WSLENV 把 token 共享進 WSL；若使用者已在 WSL 內 `claude
      setup-token` 登入，token 留空也能用。
"""

from __future__ import annotations

import os
import sys
import time
import shlex
import shutil
import logging
import subprocess

logger = logging.getLogger("claude-cli")

_CREATE_NO_WINDOW = 0x08000000  # Windows：不閃主控台視窗
_DETECT_TTL = 5.0
_detect_cache: tuple[float, dict] | None = None


class ClaudeError(RuntimeError):
    """claude 呼叫失敗（找不到、逾時、非零結束）。"""


def _win_kwargs() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _windows_candidates() -> list[str]:
    """claude 在 Windows 的常見安裝位置（GUI 行程繼承的 PATH 可能不含這些）。"""
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    cands: list[str] = []
    if appdata:
        cands.append(os.path.join(appdata, "npm", "claude.cmd"))
    cands.append(os.path.join(home, ".local", "bin", "claude.exe"))
    cands.append(os.path.join(home, ".local", "bin", "claude"))
    if local:
        cands.append(os.path.join(local, "Programs", "claude", "claude.exe"))
    return cands


def _runnable(path: str) -> bool:
    try:
        r = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, **_win_kwargs(),
        )
        return r.returncode == 0
    except Exception:
        return False


def _detect_windows() -> str | None:
    """Windows 原生 claude 的可執行路徑；找不到 / 跑不起來回 None。"""
    path = shutil.which("claude")
    if not path:
        for cand in _windows_candidates():
            if os.path.isfile(cand):
                path = cand
                break
    if path and _runnable(path):
        return path
    return None


def _detect_wsl() -> bool:
    """WSL 內是否有 claude（僅 Windows 主機才探，用預設發行版）。"""
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(
            ["wsl", "-e", "bash", "-lc", "command -v claude"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=_CREATE_NO_WINDOW,
        )
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return False


def detect(force: bool = False) -> dict:
    """回傳 {available, mode, path, message}。mode: 'windows' | 'wsl' | None。含快取。"""
    global _detect_cache
    now = time.time()
    if not force and _detect_cache and now - _detect_cache[0] < _DETECT_TTL:
        return _detect_cache[1]

    win = _detect_windows()
    if win:
        info = {"available": True, "mode": "windows", "path": win,
                "message": "Claude CLI（Windows 原生）已就緒"}
    elif _detect_wsl():
        info = {"available": True, "mode": "wsl", "path": "wsl",
                "message": "Claude CLI（WSL）已就緒"}
    else:
        info = {"available": False, "mode": None, "path": "",
                "message": "找不到 Claude CLI，請於 Windows 或 WSL 安裝後重試"}

    _detect_cache = (now, info)
    return info


def available() -> bool:
    return detect()["available"]


def run(prompt: str, *, model: str = "opus", token: str = "", timeout: float = 600.0) -> str:
    """以 `claude -p` 跑一次（prompt 走 stdin），回傳輸出文字。失敗丟 ClaudeError。"""
    info = detect()
    if not info["available"]:
        raise ClaudeError("找不到 Claude CLI（Windows / WSL 皆未偵測到）")

    args = ["-p", "--model", model, "--output-format", "text",
            "--dangerously-skip-permissions"]
    env = dict(os.environ)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    if info["mode"] == "windows":
        cmd = [info["path"], *args]
        kwargs = _win_kwargs()
    else:
        # WSL：用 WSLENV 把 token 共享進 WSL（/u = 由 Windows 帶往 WSL）；prompt 仍走 stdin。
        if token:
            prev = env.get("WSLENV", "")
            env["WSLENV"] = (prev + ":" if prev else "") + "CLAUDE_CODE_OAUTH_TOKEN/u"
        inner = "claude " + " ".join(shlex.quote(a) for a in args)
        cmd = ["wsl", "-e", "bash", "-lc", inner]
        kwargs = {"creationflags": _CREATE_NO_WINDOW}

    try:
        r = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env, **kwargs,
        )
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"claude 逾時（{int(timeout)}s）已中止")
    except FileNotFoundError as e:
        raise ClaudeError(f"無法執行 claude：{e}")

    if r.returncode != 0:
        raise ClaudeError((r.stderr or "").strip() or f"claude 結束碼 {r.returncode}")
    return (r.stdout or "").strip()
