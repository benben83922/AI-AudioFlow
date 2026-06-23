#!/usr/bin/env python3
"""STT worker — 輪詢錄音資料夾，送 whisper 容器轉譯，逐字稿原子化寫出。

此模組是 worker 的「唯一真相」。兩種啟動方式都走這支：
    - 打包後：主 exe 以 `app.exe --worker` 重新拉起自己（內嵌 worker 模式）。
    - 原始碼：`python -m src.main --worker`（或 stt-worker/worker.py 薄殼）。

與錄音端的交接約定：
    - 只處理已完整落地的檔（錄音端用 temp → os.replace 原子改名）。
    - 略過點開頭檔與 .part 暫存檔，只收 recording_*.wav 等完成檔。
逐字稿輸出同樣採原子化寫入（temp → os.replace），供 openclaw 後續消費。

設定全走環境變數（由 app 啟動時帶入；見 stt-worker/.env.example）。
"""

from __future__ import annotations

import os
import sys
import time
import json
import wave
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
    # force：worker 由 `python -m src.main --worker` 啟動，src/main.py 在模組載入時已先
    # basicConfig 給 root 裝了 StreamHandler，使這裡的 filename 變 no-op。force=True 強制
    # 移除既有 handler 改裝 FileHandler，logs 才會真正寫進 STT_LOG_FILE。
    force=True,
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
    # ── 後端選擇與 native（無 Docker）模式參數 ──
    backend: str = "docker"        # docker（HTTP→whisper 容器）| native（本機 faster-whisper）
    results_dir: Path | None = None        # native：會議紀錄 .md 輸出（docker 模式由 openclaw 負責）
    whisper_model: str = "large-v3-turbo"  # native faster-whisper 模型
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    download_root: str | None = None       # native 模型快取 / 隨包附帶目錄
    claude_model: str = "opus"             # native 整理用 claude -p 模型
    claude_token: str = ""
    claude_timeout: float = 600.0
    summary_prompt: str = ""               # 自訂會議紀錄 prompt（空＝預設）
    # 自動 vs 手動：False＝每檔需「轉逐字稿」請求標記才轉譯，整理一律留給 app（按鈕）
    auto: bool = True

    @staticmethod
    def from_env() -> "Config":
        outbox = Path(os.environ.get("STT_OUTBOX_DIR", "~/stt-outbox")).expanduser()
        done_raw = os.environ.get("STT_DONE_DIR", "").strip()
        backend = os.environ.get("STT_BACKEND", "docker").strip().lower()
        results_raw = os.environ.get("STT_RESULTS_DIR", "").strip()
        download_root = os.environ.get("WHISPER_DOWNLOAD_ROOT", "").strip()
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
            backend=backend if backend in ("docker", "native") else "docker",
            results_dir=Path(results_raw).expanduser() if results_raw else None,
            whisper_model=os.environ.get("WHISPER_MODEL", "large-v3-turbo"),
            whisper_device=os.environ.get("WHISPER_DEVICE", "cpu"),
            whisper_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
            download_root=download_root or None,
            claude_model=os.environ.get("CLAUDE_MODEL", "opus"),
            claude_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            claude_timeout=float(os.environ.get("CLAUDE_TIMEOUT", "600")),
            summary_prompt=os.environ.get("SUMMARY_PROMPT", ""),
            auto=os.environ.get("STT_AUTO", "1").strip().lower() not in ("0", "false", "no"),
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


def _transcribe_native(cfg: Config, audio: Path) -> str:
    """native 模式：本機 faster-whisper in-process 轉譯（不需 Docker / HTTP）。"""
    try:
        from src import stt_engine
    except ImportError:
        import stt_engine                       # 打包入口為頂層模組時
    return stt_engine.transcribe(
        audio, model=cfg.whisper_model, device=cfg.whisper_device,
        compute_type=cfg.whisper_compute_type, language=cfg.language,
        download_root=cfg.download_root,
    )


def write_transcript(cfg: Config, audio: Path, content: str) -> Path:
    """原子化寫入逐字稿（temp → os.replace）。"""
    out = _transcript_path(cfg, audio)
    tmp = out.with_name(f".{out.name}.part")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out)
    return out


def _audio_seconds(audio: Path) -> float | None:
    """讀 wav 檔頭取得音訊長度（秒）；非 wav 或失敗回 None。"""
    if audio.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(audio), "rb") as w:
            rate = w.getframerate()
            if rate:
                return w.getnframes() / rate
    except Exception:
        pass
    return None


def _progress_path(cfg: Config, audio: Path) -> Path:
    """進度標記檔（點開頭隱藏檔，app 端讀它算「正在轉譯哪個檔、跑多久了」）。"""
    return cfg.outbox_dir / f".{audio.stem}.progress"


def write_progress(cfg: Config, audio: Path) -> None:
    """開始轉譯某檔時寫進度標記：記下開始時間與音訊長度（供前端估算 %）。"""
    data = {"started": time.time(), "audio_seconds": _audio_seconds(audio)}
    try:
        _progress_path(cfg, audio).write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        logger.warning("寫入進度標記失敗：%s", e)


def clear_progress(cfg: Config, audio: Path) -> None:
    try:
        _progress_path(cfg, audio).unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("清除進度標記失敗：%s", e)


# ── 手動觸發：轉譯請求標記（app 寫入 watch_dir，worker 取走轉譯後消費）──

def _request_path(cfg: Config, audio: Path) -> Path:
    """某錄音的「轉逐字稿」請求標記（點開頭隱藏檔，不會被 is_candidate 當成音檔）。"""
    return cfg.watch_dir / f".{audio.stem}.transcribe.request"


def transcribe_requested(cfg: Config, audio: Path) -> bool:
    return _request_path(cfg, audio).exists()


def clear_request(cfg: Config, audio: Path) -> None:
    try:
        _request_path(cfg, audio).unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("清除轉譯請求標記失敗：%s", e)


def archive_source(cfg: Config, audio: Path) -> None:
    if cfg.done_dir is None:
        return
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(audio, cfg.done_dir / audio.name)
    except OSError as e:
        # 跨檔案系統（done_dir 與來源不同磁碟）無法 rename；此時放棄搬移、保留原檔
        logger.warning("無法搬移來源檔到 done（可能跨磁碟）：%s — 保留原檔", e)


def process_summary(cfg: Config, stem: str) -> None:
    """native 模式：逐字稿 → 會議紀錄（claude -p）。

    docker 模式此步由 openclaw 容器負責，這裡直接略過。
    claude 不可用 / 失敗時略過，留待下一輪補掃或前端手動「生成會議紀錄」補跑——
    錄音與轉譯永不被 claude 卡住。
    """
    if cfg.backend != "native" or cfg.results_dir is None:
        return
    try:
        from src import claude_cli
        from src.prompts import build_summary_prompt
    except ImportError:
        import claude_cli                        # 打包入口為頂層模組時
        from prompts import build_summary_prompt

    out = cfg.results_dir / f"{stem}.md"
    if out.exists():
        return                                   # 冪等：已整理過
    if not claude_cli.available():
        logger.info("Claude CLI 尚未就緒，暫不整理：%s（逐字稿已保留）", stem)
        return

    txt = cfg.outbox_dir / f"{stem}.txt"
    if not txt.exists():
        return

    logger.info("整理中（claude -p）：%s", stem)
    t0 = time.time()
    try:
        md = claude_cli.run(
            build_summary_prompt(txt.read_text(encoding="utf-8"), cfg.summary_prompt),
            model=cfg.claude_model, token=cfg.claude_token, timeout=cfg.claude_timeout,
        )
        cfg.results_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.part")
        tmp.write_text(md, encoding="utf-8")
        os.replace(tmp, out)                     # 原子化落地
    except claude_cli.ClaudeError as e:
        logger.warning("整理失敗（%s）：%s — 留待下一輪重試", stem, e)
        return
    logger.info("會議紀錄完成：%s（%.1fs）", out.name, time.time() - t0)


# ── 主迴圈 ──

def process_file(cfg: Config, audio: Path) -> None:
    if already_done(cfg, audio):
        return
    # 手動模式：未收到該檔的「轉逐字稿」請求標記就不處理（等使用者按按鈕）
    if not cfg.auto and not transcribe_requested(cfg, audio):
        return
    if not is_stable(audio, cfg.stable_seconds):
        logger.debug("檔案尚未穩定，稍後再試：%s", audio.name)
        return

    logger.info("轉譯中：%s", audio.name)
    t0 = time.time()
    write_progress(cfg, audio)          # 標記「開始轉譯」，供前端顯示進度
    try:
        content = _transcribe_native(cfg, audio) if cfg.backend == "native" else transcribe(cfg, audio)
        out = write_transcript(cfg, audio, content)
    finally:
        clear_progress(cfg, audio)      # 不論成功失敗都清掉標記（失敗下輪會重寫）
        if not cfg.auto:
            clear_request(cfg, audio)   # 手動：成功 / 失敗都消費請求標記（失敗請重按）
    logger.info("完成：%s → %s（%.1fs）", audio.name, out.name, time.time() - t0)
    archive_source(cfg, audio)
    if cfg.auto and cfg.backend == "native":
        process_summary(cfg, audio.stem)   # 全自動 + native：緊接著整理（手動模式留待 app 按鈕）


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
    if cfg.backend == "native" and cfg.results_dir is not None:
        cfg.results_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.watch_dir.exists():
        logger.error("監看資料夾不存在：%s", cfg.watch_dir)
        return 1

    stop = _Stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(stop, "flag", True))

    logger.info("啟動 — backend=%s watch=%s outbox=%s whisper=%s 格式=%s 輪詢=%ss",
                cfg.backend, cfg.watch_dir, cfg.outbox_dir, cfg.whisper_url,
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

        # native + 全自動：補掃有逐字稿但缺會議紀錄者（手動模式整理一律由 app 按鈕觸發）
        if cfg.auto and cfg.backend == "native" and cfg.results_dir is not None and not stop.flag:
            try:
                for txt in sorted(cfg.outbox_dir.glob("*.txt")):
                    if stop.flag:
                        break
                    if not (cfg.results_dir / f"{txt.stem}.md").exists():
                        process_summary(cfg, txt.stem)
            except Exception as e:
                logger.error("整理補掃錯誤：%s", e)

        # 可被中斷的等待
        for _ in range(int(cfg.poll_interval * 10)):
            if stop.flag:
                break
            time.sleep(0.1)

    logger.info("結束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
