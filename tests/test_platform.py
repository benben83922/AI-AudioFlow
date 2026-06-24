"""平台偵測 + STT worker 單例鎖／漂移／握手探測（stt_lock）。

來源：_platform.detect_platform / stt_lock.acquire|probe|running_port
"""
import socket

import pytest

from src.bridge import _platform
from src import stt_lock


# ── 平台偵測 ──

def test_detect_platform_value():
    assert _platform.detect_platform() in ("windows", "wsl", "linux")


def test_uses_windows_host_consistency():
    plat = _platform.detect_platform()
    assert _platform.uses_windows_host() == (plat in ("windows", "wsl"))


# ── 單例鎖 / 漂移 / 握手（stt_lock）──

@pytest.fixture
def base_port():
    """取一個目前空著的 port 當 base，避免撞到真實 worker 的 47654。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_acquire_then_probe_and_running(base_port):
    """綁定成功 → 握手回覆，probe / running_port 都認得出自己。"""
    sock, port, status = stt_lock.acquire(base_port, span=3)
    try:
        assert status == "acquired" and port == base_port
        assert stt_lock.probe(port) is True
        assert stt_lock.running_port(base_port, span=3) == port
    finally:
        sock.close()


def test_second_acquire_detects_running(base_port):
    """同 base 再 acquire → 偵測到我們的 worker（握手）→ status=running。"""
    sock, port, status = stt_lock.acquire(base_port, span=3)
    try:
        assert status == "acquired"
        sock2, port2, status2 = stt_lock.acquire(base_port, span=3)
        assert status2 == "running" and sock2 is None and port2 == base_port
    finally:
        sock.close()


def test_drifts_past_stranger(base_port):
    """陌生程式（不回握手）佔住 base → worker 漂移到下一個空 port。"""
    stranger = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stranger.bind(("127.0.0.1", base_port))
    stranger.listen(1)                       # 監聽但不回 magic = 陌生程式
    sock = None
    try:
        sock, port, status = stt_lock.acquire(base_port, span=3)
        assert status == "acquired"
        assert port == base_port + 1         # 跳過陌生程式
        assert stt_lock.probe(base_port) is False   # 陌生程式不被誤認
        assert stt_lock.running_port(base_port, span=3) == port
    finally:
        stranger.close()
        if sock:
            sock.close()


def test_probe_free_port_is_false(base_port):
    """沒人聽的 port → probe False。"""
    assert stt_lock.probe(base_port) is False
