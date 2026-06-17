#!/usr/bin/env python3
"""STT worker — 輪詢錄音資料夾，送 whisper 容器轉譯，逐字稿原子化寫出。

獨立元件，設計在 WSL2 原生執行：
    錄音 app → D:\\record ──(本 worker 輪詢)──▶ whisper 容器 (HTTP) ──▶ ~/stt-outbox

與錄音端的交接約定：
    - 只處理已完整落地的檔（錄音端用 temp → os.replace 原子改名）。
    - 略過點開頭檔與 .part 暫存檔，只收 recording_*.wav 等完成檔。
逐字稿輸出同樣採原子化寫入（temp → os.replace），供 openclaw 後續消費。

設定全走環境變數，見 .env.example。
"""

from __future__ import annotations

import os
import sys
import time
import signal
import socket
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

_log_file = os.environ.get("STT_LOG_FILE")
logging.basicConfig(
    level=os.environ.get("STT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] stt-worker: %(message)s",
    **({"filename": _log_file} if _log_file else {}),
)
logger = logging.getLogger("stt-worker")

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


@dataclass(frozen=True)
class Config:
    watch_dir: Path
    outbox_dir: Path
    done_dir: Path | None          # 若設定，處理後把來源檔移到這裡（封存）
    whisper_url: str               # 例：http://localhost:9000
    whisper_api_key: str | None
    model: str                     # OpenAI 相容欄位，多數 server 會忽略（模型由容器 env 決定）
    language: str | None           # 例：zh；None = 讓 server 自動偵測
    response_format: str           # text | json | verbose_json | srt | vtt
    poll_interval: float           # 輪詢秒數
    stable_seconds: float          # 檔案大小需連續穩定的秒數（完整性雙保險）
    request_timeout: float         # 單檔 HTTP 逾時（長錄音要夠大）
    max_retries: int
    lock_port: int                 # 單例互斥用的 localhost port

    @staticmethod
    def from_env() -> "Config":
        outbox = Path(os.environ.get("STT_OUTBOX_DIR", "~/stt-outbox")).expanduser()
        done_raw = os.environ.get("STT_DONE_DIR", "").strip()
        return Config(
            watch_dir=Path(os.environ.get("STT_WATCH_DIR", "/mnt/d/record")).expanduser(),
            outbox_dir=outbox,
            done_dir=Path(done_raw).expanduser() if done_raw else None,
            whisper_url=os.environ.get("WHISPER_URL", "http://localhost:9000").rstrip("/"),
            whisper_api_key=os.environ.get("WHISPER_API_KEY") or None,
            model=os.environ.get("STT_MODEL", "whisper-1"),
            language=os.environ.get("STT_LANGUAGE") or None,
            response_format=os.environ.get("STT_RESPONSE_FORMAT", "text"),
            poll_interval=float(os.environ.get("STT_POLL_INTERVAL", "5")),
            stable_seconds=float(os.environ.get("STT_STABLE_SECONDS", "2")),
            request_timeout=float(os.environ.get("STT_REQUEST_TIMEOUT", "600")),
            max_retries=int(os.environ.get("STT_MAX_RETRIES", "3")),
            lock_port=int(os.environ.get("STT_LOCK_PORT", "47654")),
        )


# ── 完成標記 / 過濾 ──

def _output_ext(cfg: Config) -> str:
    return {"json": ".json", "verbose_json": ".json", "srt": ".srt", "vtt": ".vtt"}.get(
        cfg.response_format, ".txt"
    )


def _transcript_path(cfg: Config, audio: Path) -> Path:
    return cfg.outbox_dir / (audio.stem + _output_ext(cfg))


def is_candidate(path: Path) -> bool:
    """是否為要處理的音檔（略過暫存 / 隱藏 / 非音訊）。"""
    name = path.name
    if name.startswith("."):            # 隱藏 / .recording_xxx.wav.part
        return False
    if path.suffix.lower() == ".part":
        return False
    if path.suffix.lower() not in AUDIO_EXTS:
        return False
    return path.is_file()


def already_done(cfg: Config, audio: Path) -> bool:
    """冪等：逐字稿已存在就跳過（重啟不重跑）。"""
    return _transcript_path(cfg, audio).exists()


def is_stable(path: Path, stable_seconds: float) -> bool:
    """完整性雙保險：檔案大小在 stable_seconds 內未變動才視為寫完。

    錄音端已用原子改名，理論上看到的就是完整檔；此檢查防止其他來源
    （手動複製等）放進半截檔。
    """
    try:
        size1 = path.stat().st_size
    except FileNotFoundError:
        return False
    if size1 == 0:
        return False
    time.sleep(min(stable_seconds, 2.0))
    try:
        return path.stat().st_size == size1
    except FileNotFoundError:
        return False


# ── 轉譯 ──

def transcribe(cfg: Config, audio: Path) -> str:
    """POST 音檔到 whisper 的 OpenAI 相容端點，回傳逐字稿文字內容。"""
    url = f"{cfg.whisper_url}/v1/audio/transcriptions"
    headers = {}
    if cfg.whisper_api_key:
        headers["Authorization"] = f"Bearer {cfg.whisper_api_key}"

    data = {"model": cfg.model, "response_format": cfg.response_format}
    if cfg.language:
        data["language"] = cfg.language

    last_err: Exception | None = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            with open(audio, "rb") as f:
                files = {"file": (audio.name, f, "application/octet-stream")}
                with httpx.Client(timeout=cfg.request_timeout) as client:
                    resp = client.post(url, headers=headers, data=data, files=files)
            resp.raise_for_status()
            # text/srt/vtt 直接回字串；json 系列回 JSON，取 text 欄位但保留原文
            if cfg.response_format in ("json", "verbose_json"):
                return resp.text
            return resp.text
        except Exception as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            logger.warning("轉譯失敗（第 %d/%d 次）：%s — %ds 後重試",
                           attempt, cfg.max_retries, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"轉譯重試 {cfg.max_retries} 次仍失敗：{last_err}")


def write_transcript(cfg: Config, audio: Path, content: str) -> Path:
    """原子化寫入逐字稿（temp → os.replace）。"""
    out = _transcript_path(cfg, audio)
    tmp = out.with_name(f".{out.name}.part")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out)
    return out


def archive_source(cfg: Config, audio: Path) -> None:
    if cfg.done_dir is None:
        return
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(audio, cfg.done_dir / audio.name)
    except OSError as e:
        # 跨檔案系統（done_dir 與來源不同磁碟）無法 rename；此時放棄搬移、保留原檔
        logger.warning("無法搬移來源檔到 done（可能跨磁碟）：%s — 保留原檔", e)


# ── 主迴圈 ──

def process_file(cfg: Config, audio: Path) -> None:
    if already_done(cfg, audio):
        return
    if not is_stable(audio, cfg.stable_seconds):
        logger.debug("檔案尚未穩定，稍後再試：%s", audio.name)
        return

    logger.info("轉譯中：%s", audio.name)
    t0 = time.time()
    content = transcribe(cfg, audio)
    out = write_transcript(cfg, audio, content)
    logger.info("完成：%s → %s（%.1fs）", audio.name, out.name, time.time() - t0)
    archive_source(cfg, audio)


class _Stop:
    flag = False


def _acquire_singleton(port: int) -> socket.socket | None:
    """綁定 localhost port 當互斥鎖；成功回傳 socket（需保持參照），失敗回傳 None。

    程序結束時 OS 自動釋放 port，不會留下殘留鎖檔。app 端可用 connect 偵測是否已在跑。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))   # 不設 SO_REUSEADDR，確保獨佔
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def main() -> int:
    cfg = Config.from_env()

    # 單例守衛：已有 worker 在跑就直接結束（app 每次啟動都會嘗試拉起，靠此去重）
    lock = _acquire_singleton(cfg.lock_port)
    if lock is None:
        logger.info("另一個 STT worker 已在執行（port %d 被占用），本實例結束。", cfg.lock_port)
        return 0

    cfg.outbox_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.watch_dir.exists():
        logger.error("監看資料夾不存在：%s", cfg.watch_dir)
        return 1

    stop = _Stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(stop, "flag", True))

    logger.info("啟動 — watch=%s outbox=%s whisper=%s 格式=%s 輪詢=%ss",
                cfg.watch_dir, cfg.outbox_dir, cfg.whisper_url,
                cfg.response_format, cfg.poll_interval)

    while not stop.flag:
        try:
            for path in sorted(cfg.watch_dir.iterdir()):
                if stop.flag:
                    break
                if not is_candidate(path):
                    continue
                try:
                    process_file(cfg, path)
                except Exception as e:
                    logger.error("處理 %s 失敗：%s", path.name, e)
        except Exception as e:
            logger.error("輪詢迴圈錯誤：%s", e)

        # 可被中斷的等待
        for _ in range(int(cfg.poll_interval * 10)):
            if stop.flag:
                break
            time.sleep(0.1)

    logger.info("結束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
