#!/usr/bin/env python3
"""處理 worker（純 Windows 版）— 錄音檔 → 逐字稿 → 會議紀錄的雙階段原生 pipeline。

與原系統差異：不再走 whisper 容器 + llm-service + openclaw，全程在本機 Windows
原生執行：
    階段 1（轉譯）：faster-whisper 在行程內轉譯 → 逐字稿 .txt
    階段 2（整理）：直接呼叫 `claude -p`（雙偵測：Windows 原生優先，WSL 後備）→ 會議紀錄 .md

兩種啟動方式都走這支：
    - 打包後：主 exe 以 `app.exe --worker` 重新拉起自己（內嵌 worker 模式）。
    - 原始碼：`python -m src.main --worker`。

設計沿用原 worker 的穩健性：輪詢（非 inotify）、完整檔判斷（.part / 點開頭 / 大小穩定）、
原子化寫入（temp → os.replace）、冪等（已有產出就跳過）、單例守衛（綁 localhost port）。

階段 2 的閘門：claude 不可用時，逐字稿仍正常產出，整理留待 claude 就緒後（下一輪
掃描或使用者於前端手動「生成會議紀錄」）補跑——錄音與轉譯永不被 claude 卡住。

設定全走環境變數（由 app 啟動時帶入；見 config.example.json / _worker.py）。
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

_log_file = os.environ.get("STT_LOG_FILE")
logging.basicConfig(
    level=os.environ.get("STT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] worker: %(message)s",
    **({"filename": _log_file} if _log_file else {}),
)
logger = logging.getLogger("worker")

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


@dataclass(frozen=True)
class Config:
    watch_dir: Path                # 錄音檔資料夾
    transcripts_dir: Path          # 逐字稿輸出 .txt
    results_dir: Path              # 會議紀錄輸出 .md
    # STT（faster-whisper）
    model: str
    device: str
    compute_type: str
    download_root: str | None
    language: str | None
    # 整理（claude -p）
    claude_model: str
    claude_token: str
    claude_timeout: float
    # 迴圈 / 守衛
    poll_interval: float
    stable_seconds: float
    lock_port: int

    @staticmethod
    def from_env() -> "Config":
        data_root = Path(os.environ.get("AUDIOFLOW_DATA_ROOT", ".")).expanduser()
        transcripts = os.environ.get("STT_TRANSCRIPTS_DIR", "").strip()
        results = os.environ.get("STT_RESULTS_DIR", "").strip()
        download_root = os.environ.get("WHISPER_DOWNLOAD_ROOT", "").strip()
        return Config(
            watch_dir=Path(os.environ.get("STT_WATCH_DIR", str(data_root / "recordings"))).expanduser(),
            transcripts_dir=Path(transcripts or str(data_root / "transcripts")).expanduser(),
            results_dir=Path(results or str(data_root / "results")).expanduser(),
            model=os.environ.get("WHISPER_MODEL", "large-v3-turbo"),
            device=os.environ.get("WHISPER_DEVICE", "cpu"),
            compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
            download_root=download_root or None,
            language=os.environ.get("STT_LANGUAGE") or None,
            claude_model=os.environ.get("CLAUDE_MODEL", "opus"),
            claude_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            claude_timeout=float(os.environ.get("CLAUDE_TIMEOUT", "600")),
            poll_interval=float(os.environ.get("STT_POLL_INTERVAL", "5")),
            stable_seconds=float(os.environ.get("STT_STABLE_SECONDS", "2")),
            lock_port=int(os.environ.get("STT_LOCK_PORT", "47654")),
        )


# ── 過濾 / 冪等 ──

def is_candidate(path: Path) -> bool:
    """是否為要處理的音檔（略過暫存 / 隱藏 / 非音訊）。"""
    name = path.name
    if name.startswith("."):                 # 隱藏 / .recording_xxx.wav.part
        return False
    if path.suffix.lower() == ".part":
        return False
    if path.suffix.lower() not in AUDIO_EXTS:
        return False
    return path.is_file()


def transcript_path(cfg: Config, audio: Path) -> Path:
    return cfg.transcripts_dir / (audio.stem + ".txt")


def result_path(cfg: Config, stem: str) -> Path:
    return cfg.results_dir / (stem + ".md")


def is_stable(path: Path, stable_seconds: float) -> bool:
    """完整性雙保險：檔案大小在 stable_seconds 內未變動才視為寫完。"""
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


def _atomic_write(out: Path, content: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.part")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out)


# ── 進度標記（供前端估算轉譯 %）──

def _audio_seconds(audio: Path) -> float | None:
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
    return cfg.transcripts_dir / f".{audio.stem}.progress"


def write_progress(cfg: Config, audio: Path) -> None:
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


# ── 階段 1：轉譯（faster-whisper in-process）──

def do_transcribe(cfg: Config, audio: Path) -> str:
    import src.stt_engine as stt_engine
    return stt_engine.transcribe(
        audio, model=cfg.model, device=cfg.device, compute_type=cfg.compute_type,
        language=cfg.language, download_root=cfg.download_root,
    )


def process_transcription(cfg: Config, audio: Path) -> bool:
    """音檔 → 逐字稿。回傳是否「剛產出」逐字稿（供觸發接續整理）。"""
    out = transcript_path(cfg, audio)
    if out.exists():
        return False
    if not is_stable(audio, cfg.stable_seconds):
        logger.debug("檔案尚未穩定，稍後再試：%s", audio.name)
        return False

    logger.info("轉譯中：%s", audio.name)
    t0 = time.time()
    write_progress(cfg, audio)
    try:
        text = do_transcribe(cfg, audio)
        _atomic_write(out, text)
    finally:
        clear_progress(cfg, audio)
    logger.info("逐字稿完成：%s → %s（%.1fs）", audio.name, out.name, time.time() - t0)
    return True


# ── 階段 2：整理（claude -p，雙偵測）──

def process_summary(cfg: Config, stem: str) -> None:
    """逐字稿 → 會議紀錄。claude 不可用 / 失敗則略過，留待下一輪或前端手動補跑。"""
    import src.claude_cli as claude_cli
    from src.prompts import build_summary_prompt

    out = result_path(cfg, stem)
    if out.exists():
        return
    if not claude_cli.available():
        logger.info("Claude CLI 尚未就緒，暫不整理：%s（逐字稿已保留）", stem)
        return

    txt = transcript_path(cfg, Path(stem))
    if not txt.exists():
        return

    logger.info("整理中（claude -p）：%s", stem)
    t0 = time.time()
    try:
        md = claude_cli.run(
            build_summary_prompt(txt.read_text(encoding="utf-8")),
            model=cfg.claude_model, token=cfg.claude_token, timeout=cfg.claude_timeout,
        )
        _atomic_write(out, md)
    except claude_cli.ClaudeError as e:
        logger.warning("整理失敗（%s）：%s — 留待下一輪重試", stem, e)
        return
    logger.info("會議紀錄完成：%s（%.1fs）", out.name, time.time() - t0)


# ── 主迴圈 ──

class _Stop:
    flag = False


def _acquire_singleton(port: int) -> socket.socket | None:
    """綁定 localhost port 當互斥鎖；成功回 socket（需保持參照），失敗回 None。"""
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

    lock = _acquire_singleton(cfg.lock_port)
    if lock is None:
        logger.info("另一個 worker 已在執行（port %d 被占用），本實例結束。", cfg.lock_port)
        return 0

    cfg.transcripts_dir.mkdir(parents=True, exist_ok=True)
    cfg.results_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.watch_dir.exists():
        logger.error("監看資料夾不存在：%s", cfg.watch_dir)
        return 1

    stop = _Stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(stop, "flag", True))

    logger.info(
        "啟動 — watch=%s transcripts=%s results=%s STT=%s/%s/%s claude=%s 輪詢=%ss",
        cfg.watch_dir, cfg.transcripts_dir, cfg.results_dir,
        cfg.model, cfg.device, cfg.compute_type, cfg.claude_model, cfg.poll_interval,
    )

    while not stop.flag:
        try:
            # 階段 1：掃錄音 → 缺逐字稿者轉譯（剛產出的接著整理）
            for path in sorted(cfg.watch_dir.iterdir()):
                if stop.flag:
                    break
                if not is_candidate(path):
                    continue
                try:
                    if process_transcription(cfg, path):
                        process_summary(cfg, path.stem)
                except Exception as e:
                    logger.error("轉譯 %s 失敗：%s", path.name, e)

            # 階段 2 補掃：有逐字稿但沒會議紀錄者（如 claude 先前不可用 / 失敗）補整理
            if not stop.flag:
                try:
                    for txt in sorted(cfg.transcripts_dir.glob("*.txt")):
                        if stop.flag:
                            break
                        if not result_path(cfg, txt.stem).exists():
                            process_summary(cfg, txt.stem)
                except Exception as e:
                    logger.error("整理補掃錯誤：%s", e)
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
