"""STT worker 單例鎖 + port 探測 —— worker 端綁定、app 端偵測共用的唯一真相。

port 同時身兼兩職:單例互斥鎖 + 存活偵測信標。為了能分辨「我們的 worker」與
「剛好佔用同一 port 的陌生程式」,綁定方會在 port 上回覆一個 magic 握手字串;
探測/偵測方連上去讀到 magic 才算「worker 在跑」。

被陌生程式佔用時,綁定方在 [base, base+span) 範圍內漂移到下一個空 port;app 端
偵測時同樣掃描這個範圍,靠握手辨識認出 worker —— 故不需額外的 port 檔同步,兩邊
各自掃描即可達成一致(handshake 確保只會認到自己的 worker,不會誤判陌生程式)。

只用標準函式庫(socket / threading),app 端 import 也很輕量。
"""
from __future__ import annotations

import socket
import logging
import threading

logger = logging.getLogger(__name__)

MAGIC = b"AUDIOFLOW_STT_WORKER_V1\n"   # 握手字串;改版時連同偵測端一起改
DEFAULT_LOCK_PORT = 47654
PORT_SPAN = 10                          # 漂移範圍:47654..47663
_HOST = "127.0.0.1"
_PROBE_TIMEOUT = 0.4                     # 探測連線逾時(秒)


def probe(port: int) -> bool:
    """連上 port 讀握手字串:是我們的 worker → True;陌生程式 / 沒人聽 → False。"""
    try:
        with socket.create_connection((_HOST, port), timeout=_PROBE_TIMEOUT) as s:
            s.settimeout(_PROBE_TIMEOUT)
            return s.recv(len(MAGIC)) == MAGIC
    except OSError:
        return False


def running_port(base_port: int = DEFAULT_LOCK_PORT, span: int = PORT_SPAN) -> int | None:
    """掃描範圍,回傳「我們的 worker」實際監聽的 port;都沒有則 None。"""
    for port in range(base_port, base_port + span):
        if probe(port):
            return port
    return None


def is_running(base_port: int = DEFAULT_LOCK_PORT, span: int = PORT_SPAN) -> bool:
    return running_port(base_port, span) is not None


def _serve_handshake(sock: socket.socket) -> None:
    """背景接受連線並回覆 magic;探測方靠此辨識「這是我們的 worker」。

    worker 結束時 socket 關閉 → accept 拋 OSError → 執行緒自然收束。
    """
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            conn.sendall(MAGIC)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def acquire(base_port: int = DEFAULT_LOCK_PORT, span: int = PORT_SPAN):
    """嘗試取得單例鎖,必要時在 [base, base+span) 內漂移避開陌生程式。

    回傳 (sock, port, status):
        status="acquired" —— 成功綁定並啟動握手回覆;sock 需保持參照(GC 會釋放
                             port),port 為實際綁定的 port。
        status="running"  —— 範圍內已有「我們的 worker」(探測到 magic)→ 呼叫方應結束。
        status="blocked"  —— 範圍內全被陌生程式佔滿,無法啟動 → 呼叫方應報錯結束。

    綁定不設 SO_REUSEADDR,確保獨佔;程序結束時 OS 自動釋放,不留殘留鎖。
    """
    for port in range(base_port, base_port + span):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((_HOST, port))
            s.listen(8)
        except OSError:
            s.close()
            if probe(port):
                return None, port, "running"   # 已有真 worker → 去重結束
            continue                            # 陌生程式佔用 → 試下一個 port
        threading.Thread(target=_serve_handshake, args=(s,),
                         name="stt-lock-handshake", daemon=True).start()
        return s, port, "acquired"
    return None, base_port, "blocked"
