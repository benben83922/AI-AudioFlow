import os
import time
import threading
import logging
from datetime import datetime
from pathlib import Path

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)

# 格式化秒數為 MM:SS
def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

# 格式化 bytes 為可讀大小
def _fmt_size(bytes_: int) -> str:
    if bytes_ < 1024 * 1024:
        return f"{bytes_ / 1024:.1f} KB"
    return f"{bytes_ / 1024 / 1024:.1f} MB"


class RecordingMixin:
    """音訊擷取 — 錄音控制與本地檔案管理。"""

    _recording_thread: threading.Thread | None = None
    _recording_stop_event: threading.Event | None = None
    _recording_started_at: float | None = None
    _recording_filename: str | None = None

    # ── 錄音控制 ──

    def get_recording_status(self) -> dict:
        is_recording = (
            self._recording_thread is not None
            and self._recording_thread.is_alive()
        )
        duration = ""
        if is_recording and self._recording_started_at:
            elapsed = time.time() - self._recording_started_at
            duration = _fmt_duration(elapsed)

        return _ok({
            "recording": is_recording,
            "duration": duration,
            "filename": self._recording_filename or "",
        })

    def start_recording(self) -> dict:
        if self._recording_thread and self._recording_thread.is_alive():
            return _err(ErrorType.RECORDING_ACTIVE, "錄音已在進行中")

        config = self._load_config()
        local_path = config["storage"].get("local_path", "").strip()
        recordings_dir = Path(local_path) if local_path else self._recordings_dir
        recordings_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        filename = f"recording_{ts}.wav"
        filepath = recordings_dir / filename

        self._recording_stop_event = threading.Event()
        self._recording_started_at = time.time()
        self._recording_filename = filename

        def _record():
            try:
                import sounddevice as sd
                import soundfile as sf
                import numpy as np

                samplerate = 44100
                channels = 2
                frames = []

                def callback(indata, frame_count, time_info, status):
                    if status:
                        logger.warning("sounddevice status: %s", status)
                    frames.append(indata.copy())

                with sd.InputStream(samplerate=samplerate, channels=channels, callback=callback):
                    while not self._recording_stop_event.is_set():
                        self._recording_stop_event.wait(timeout=0.1)

                if frames:
                    audio = np.concatenate(frames, axis=0)
                    sf.write(str(filepath), audio, samplerate)
                    logger.info("Recording saved: %s", filepath)
            except ImportError:
                # sounddevice / soundfile 未安裝時，建立空白佔位檔
                logger.warning("sounddevice not installed, creating placeholder file")
                filepath.touch()
            except Exception as e:
                logger.error("Recording error: %s", e)

        self._recording_thread = threading.Thread(target=_record, name="audioflow-recorder", daemon=True)
        self._recording_thread.start()

        return _ok({"filename": filename, "started_at": datetime.now().isoformat()})

    def stop_recording(self) -> dict:
        if not self._recording_thread or not self._recording_thread.is_alive():
            return _err(ErrorType.RECORDING_INACTIVE, "目前沒有進行中的錄音")

        self._recording_stop_event.set()
        self._recording_thread.join(timeout=5)

        elapsed = time.time() - (self._recording_started_at or time.time())
        filename = self._recording_filename or ""

        self._recording_thread = None
        self._recording_stop_event = None
        self._recording_started_at = None
        self._recording_filename = None

        return _ok({"filename": filename, "duration": _fmt_duration(elapsed)})

    def split_recording(self) -> dict:
        if not self._recording_thread or not self._recording_thread.is_alive():
            return _err(ErrorType.RECORDING_INACTIVE, "目前沒有進行中的錄音")

        # 停止目前錄音並立即開始新的
        self.stop_recording()
        return self.start_recording()

    # ── 檔案管理 ──

    def list_recordings(self) -> dict:
        config = self._load_config()
        local_path = config["storage"].get("local_path", "").strip()
        recordings_dir = Path(local_path) if local_path else self._recordings_dir

        if not recordings_dir.exists():
            return _ok([])

        items = []
        for path in sorted(recordings_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.suffix.lower() not in (".wav", ".mp3", ".m4a"):
                continue
            stat = path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
            now = datetime.now()
            if mtime.date() == now.date():
                time_str = f"今天 {mtime.strftime('%H:%M')}"
            elif (now - mtime).days == 1:
                time_str = f"昨天 {mtime.strftime('%H:%M')}"
            else:
                time_str = mtime.strftime("%m/%d %H:%M")

            items.append({
                "id": path.name,
                "name": path.name,
                "size": _fmt_size(stat.st_size),
                "duration": "--:--",
                "status": "done",
                "time": time_str,
                "path": str(path),
            })

        return _ok(items)

    def delete_recording(self, filename: str) -> dict:
        config = self._load_config()
        local_path = config["storage"].get("local_path", "").strip()
        recordings_dir = Path(local_path) if local_path else self._recordings_dir

        # 防止路徑穿越攻擊
        target = (recordings_dir / filename).resolve()
        if not str(target).startswith(str(recordings_dir.resolve())):
            return _err(ErrorType.VALIDATION, "非法的檔案路徑")

        if not target.exists():
            return _err(ErrorType.NOT_FOUND, f"找不到檔案：{filename}")

        try:
            target.unlink()
            logger.info("Deleted recording: %s", target)
            return _ok()
        except Exception as e:
            return _err(ErrorType.INTERNAL, str(e))
