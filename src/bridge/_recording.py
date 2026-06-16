import os
import sys
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
                import soundfile as sf
                import numpy as np

                SAMPLERATE = 44100

                if sys.platform == "win32":
                    audio = _record_windows(SAMPLERATE)
                else:
                    audio = _record_linux(SAMPLERATE)

                if audio is not None:
                    sf.write(str(filepath), audio.astype(np.float32), SAMPLERATE)
                    logger.info("Recording saved: %s", filepath)

            except (ImportError, OSError) as e:
                logger.error("Audio library unavailable: %s", e)
                if self._window:
                    self._window.evaluate_js(
                        f"showToast('音訊裝置錯誤：{str(e).replace(chr(39), '')}', 'error', 8000)"
                    )
            except Exception as e:
                logger.error("Recording error: %s", e)

        def _record_windows(samplerate: int):
            import pyaudiowpatch as pyaudio
            import sounddevice as sd
            import numpy as np

            frames_sys, frames_mic = [], []
            p = pyaudio.PyAudio()
            sys_stream = mic_stream = None

            # ── 系統音（WASAPI loopback）──
            try:
                wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                default_out  = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
                loopback_dev = next(
                    (lb for lb in p.get_loopback_device_info_generator()
                     if default_out["name"] in lb["name"]),
                    None,
                )
                if loopback_dev:
                    lb_rate = int(loopback_dev["defaultSampleRate"])
                    lb_ch   = min(loopback_dev["maxInputChannels"], 2)

                    def sys_cb(in_data, frame_count, time_info, status):
                        arr = np.frombuffer(in_data, dtype=np.float32).reshape(-1, lb_ch)
                        if lb_ch == 1:
                            arr = np.column_stack([arr, arr])
                        if lb_rate != samplerate:
                            idx = np.clip(
                                (np.arange(max(1, int(len(arr) * samplerate / lb_rate))) * lb_rate / samplerate).astype(int),
                                0, len(arr) - 1,
                            )
                            arr = arr[idx]
                        frames_sys.append(arr)
                        return (None, pyaudio.paContinue)

                    sys_stream = p.open(
                        format=pyaudio.paFloat32,
                        channels=lb_ch, rate=lb_rate,
                        input=True, input_device_index=loopback_dev["index"],
                        frames_per_buffer=1024, stream_callback=sys_cb,
                    )
                    sys_stream.start_stream()
                    logger.info("System loopback: %s (%dHz %dch)", loopback_dev["name"], lb_rate, lb_ch)
                else:
                    logger.warning("No loopback device found for: %s", default_out["name"])
            except Exception as e:
                logger.warning("Cannot open system loopback: %s", e)

            # ── 麥克風 ──
            try:
                def mic_cb(indata, *_):
                    arr = indata.copy()
                    if arr.shape[1] == 1:
                        arr = np.column_stack([arr, arr])
                    frames_mic.append(arr)

                mic_stream = sd.InputStream(samplerate=samplerate, channels=1, callback=mic_cb)
                mic_stream.start()
                logger.info("Mic stream opened")
            except Exception as e:
                logger.warning("Cannot open mic: %s", e)

            while not self._recording_stop_event.is_set():
                self._recording_stop_event.wait(timeout=0.1)

            if sys_stream:
                sys_stream.stop_stream(); sys_stream.close()
            if mic_stream:
                mic_stream.stop(); mic_stream.close()
            p.terminate()

            has_sys, has_mic = bool(frames_sys), bool(frames_mic)
            if not has_sys and not has_mic:
                logger.warning("No audio captured")
                return None

            if has_sys and has_mic:
                a_sys = np.concatenate(frames_sys, axis=0).astype(np.float32)
                a_mic = np.concatenate(frames_mic, axis=0).astype(np.float32)
                n = max(len(a_sys), len(a_mic))
                a_sys = np.pad(a_sys, ((0, n - len(a_sys)), (0, 0)))
                a_mic = np.pad(a_mic, ((0, n - len(a_mic)), (0, 0)))
                audio = np.clip(a_sys * 0.6 + a_mic * 0.6, -1.0, 1.0)
                logger.info("Mixed sys+mic: %d samples", n)
            elif has_sys:
                audio = np.concatenate(frames_sys, axis=0).astype(np.float32)
            else:
                audio = np.concatenate(frames_mic, axis=0).astype(np.float32)
            return audio

        def _record_linux(samplerate: int):
            import sounddevice as sd
            import numpy as np

            frames = []

            def callback(indata, *_):
                frames.append(np.column_stack([indata, indata]))

            with sd.InputStream(device="rdpsource", samplerate=samplerate, channels=1, callback=callback):
                while not self._recording_stop_event.is_set():
                    self._recording_stop_event.wait(timeout=0.1)

            if not frames:
                logger.warning("No audio captured")
                return None
            return np.concatenate(frames, axis=0)

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
