"""服務啟動前置把關：Windows 下 compose 服務需 WSL 內 docker 連得到引擎。

來源：_services._service_deps_ok / _wsl_docker_ok
（針對「Docker Desktop WSL 整合未開 → llm/openclaw 起不來」加的預檢）
"""
import pytest


def _force_docker_mode(bridge, monkeypatch):
    """讓把關走 docker 路徑：模式=docker、Docker 已就緒、WSL 已就緒、token 已填。"""
    monkeypatch.setattr(bridge, "_pipeline_mode", lambda: "docker")
    monkeypatch.setattr(bridge, "_docker_state", lambda: ("ok", ""))
    monkeypatch.setattr(bridge, "_wsl_state", lambda: ("ok", ""))
    monkeypatch.setattr(bridge, "_claude_token", lambda: "tok")


def test_compose_blocked_when_wsl_docker_unreachable(bridge, monkeypatch):
    """Windows + WSL 內 docker 連不到 → llm_service 把關擋下並回 WSL 整合提示。"""
    _force_docker_mode(bridge, monkeypatch)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(bridge, "_wsl_docker_ok", lambda: (False, "WSL 內連不到 Docker 引擎：請開啟…WSL Integration…"))
    ok, msg = bridge._service_deps_ok("llm_service")
    assert ok is False
    assert "WSL Integration" in msg


def test_compose_passes_when_wsl_docker_ok(bridge, monkeypatch):
    """Windows + WSL docker 正常 + token 已填 → llm_service 把關通過。"""
    _force_docker_mode(bridge, monkeypatch)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(bridge, "_wsl_docker_ok", lambda: (True, ""))
    ok, msg = bridge._service_deps_ok("llm_service")
    assert ok is True


def test_wsl_docker_check_skipped_on_non_windows(bridge, monkeypatch):
    """非 Windows（純 Linux docker）不應呼叫 WSL 預檢（compose 直接跑本機 docker）。"""
    _force_docker_mode(bridge, monkeypatch)
    monkeypatch.setattr("sys.platform", "linux")

    def _boom():
        raise AssertionError("非 Windows 不應呼叫 _wsl_docker_ok")
    monkeypatch.setattr(bridge, "_wsl_docker_ok", _boom)
    ok, msg = bridge._service_deps_ok("openclaw")
    assert ok is True


def test_whisper_not_gated_by_wsl_docker(bridge, monkeypatch):
    """whisper 走 docker.exe，不受 WSL docker 預檢影響（即使 WSL docker 壞）。"""
    _force_docker_mode(bridge, monkeypatch)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(bridge, "_wsl_docker_ok", lambda: (False, "壞了"))
    ok, msg = bridge._service_deps_ok("whisper")
    assert ok is True   # whisper 只需 Docker 就緒，不碰 WSL docker 預檢
