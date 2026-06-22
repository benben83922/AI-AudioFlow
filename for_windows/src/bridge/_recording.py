import os
import sys
import json
import time
import threading
import logging
from datetime import datetime
from pathlib import Path

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)

# 轉譯耗時估算係數：預估轉譯秒數 ≈ 音訊秒數 × 此係數（CPU large-v3-turbo 粗估）。
# 用於前端顯示「約 N%」；可用 STT_PROGRESS_FACTOR 覆寫。實際快慢看硬體，僅供參考。
_PROGRESS_FACTOR = float(os.environ.get("STT_PROGRESS_FACTOR", "1.0"))


def _progress_markers(dir_) -> dict:
    """讀 outbox 內 .<stem>.progress，回傳 {stem: {started, audio_seconds}}。

    只有「worker 正在轉譯」的檔才有標記；沒標記又沒逐字稿 = 仍在排隊。
    """
    out: dict = {}
    if dir_ is None:
        return out
    try:
        for p in Path(dir_).iterdir():
            name = p.name
            if name.startswith(".") and name.endswith(".progress"):
                stem = name[1:-len(".progress")]
                try:
                    out[stem] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    out[stem] = {}
    except Exception:
        pass
    return out


def _calc_progress(marker: dict | None) -> dict | None:
    """由進度標記算出顯示用進度：active（轉譯中）/ 排隊中、已耗時、估算 %。"""
    if marker is None:
        return {"active": False}        # 有 transcribing 狀態但沒標記 → 排隊中
    started = marker.get("started")
    if not started:
        return {"active": True, "elapsed": "", "pct": None}
    elapsed = max(0.0, time.time() - started)
    audio_s = marker.get("audio_seconds")
    pct = None
    if audio_s and audio_s > 0:
        est = audio_s * _PROGRESS_FACTOR
        if est > 0:
            pct = min(95, int(elapsed / est * 100))   # 封頂 95%，真正完成才到 100
    return {"active": True, "elapsed": _fmt_duration(elapsed), "pct": pct}

# 格式化秒數為 MM:SS
def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

# 格式化 bytes 為可讀大小
def _fmt_size(bytes_: int) -> str:
    if bytes_ < 1024 * 1024:
        return f"{bytes_ / 1024:.1f} KB"
    return f"{bytes_ / 1024 / 1024:.1f} MB"

# 由音訊緩衝估算 VU 音量（0..1）
def _calc_level(arr) -> float:
    import numpy as np
    a = np.asarray(arr, dtype=np.float64)
    if a.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(a ** 2)))
    return min(1.0, rms * 15.0)

# 列出資料夾內某副檔名的所有檔名主幹（stem）；資料夾不存在 / 不可讀回空集合
def _stems_in(dir_, ext: str) -> set[str]:
    if dir_ is None:
        return set()
    try:
        return {p.stem for p in Path(dir_).iterdir() if p.suffix.lower() == ext}
    except Exception:
        return set()

# 讀 wav 檔頭取得時長（MM:SS）；失敗回 --:--
def _wav_duration(path: Path) -> str:
    try:
        import soundfile as sf
        info = sf.info(str(path))
        if info.samplerate:
            return _fmt_duration(info.frames / info.samplerate)
    except Exception:
        pass
    return "--:--"


class RecordingMixin:
    """音訊擷取 — 錄音控制與本地檔案管理。"""

    _recording_thread: threading.Thread | None = None
    _recording_stop_event: threading.Event | None = None
    _recording_started_at: float | None = None
    _recording_filename: str | None = None
    _recording_level: float = 0.0          # 即時音量 0..1（VU 表用）
    _audio_cache: tuple[float, bool, str] | None = None  # (時間, available, message)

    # ── 音訊裝置偵測 ──

    def _audio_input_available(self) -> tuple[bool, str]:
        """是否有可用的音訊輸入來源（麥克風，或 Windows 系統音 loopback）。

        回傳 (available, message)。含 5 秒快取，避免每次輪詢都查裝置。
        """
        now = time.time()
        if self._audio_cache and now - self._audio_cache[0] < 5.0:
            return self._audio_cache[1], self._audio_cache[2]

        available = False
        message = ""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            available = any(d.get("max_input_channels", 0) > 0 for d in devices)

            # Windows：即使沒有麥克風，系統音 WASAPI loopback 也算可錄來源
            if not available and sys.platform == "win32":
                try:
                    import pyaudiowpatch as pyaudio
                    p = pyaudio.PyAudio()
                    available = any(True for _ in p.get_loopback_device_info_generator())
                    p.terminate()
                except Exception:
                    pass

            if not available:
                message = "找不到可用的音訊輸入裝置（麥克風或系統音）"
        except Exception as e:
            message = f"音訊系統無法初始化：{e}"

        self.__class__._audio_cache = (now, available, message)
        return available, message

    def _notify_toast(self, message: str, level: str = "error", duration: int = 6000) -> None:
        """從背景錄音執行緒推一則 toast 給前端（跳脫反斜線與單引號）。"""
        if not self._window:
            return
        try:
            safe = message.replace("\\", "\\\\").replace("'", "\\'")
            self._window.evaluate_js(f"showToast('{safe}', '{level}', {int(duration)})")
        except Exception as e:
            logger.warning("推送前端訊息失敗：%s", e)

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
            "level": round(self._recording_level, 3) if is_recording else 0.0,
        })

    def start_recording(self) -> dict:
        if self._recording_thread and self._recording_thread.is_alive():
            return _err(ErrorType.RECORDING_ACTIVE, "錄音已在進行中")

        # 純 Windows 版：錄音只需音訊裝置就緒（轉譯為本機 in-process、整理在事後）。
        env = self.get_environment_status()["data"]
        if not env["can_record"]:
            return _err(ErrorType.NO_DEVICE, env["message"] or "找不到可用的音訊輸入裝置")

        config = self._load_config()
        local_path = config["storage"].get("local_path", "").strip()
        recordings_dir = Path(local_path) if local_path else self._recordings_dir
        recordings_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        filename = f"recording_{ts}.wav"
        filepath = recordings_dir / filename
        # 暫存名（與最終檔同資料夾，確保 os.replace 為原子操作）；
        # 取檔端（STT worker）的輪詢需略過 .part / 點開頭檔，只收 recording_*.wav
        tmp_path = recordings_dir / f".{filename}.part"

        self._recording_stop_event = threading.Event()
        self._recording_started_at = time.time()
        self._recording_filename = filename
        self._recording_level = 0.0

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
                    # 原子化落地：先寫暫存檔，寫完關檔後再原子改名為最終檔，
                    # 避免取檔端讀到寫一半的檔。
                    # 明確指定 WAV 格式：暫存檔副檔名是 .part，soundfile 無法由副檔名
                    # 推斷格式，必須顯式指定，否則寫檔會失敗、錄音遺失。
                    sf.write(str(tmp_path), audio.astype(np.float32), SAMPLERATE, format="WAV")
                    os.replace(tmp_path, filepath)
                    logger.info("Recording saved: %s", filepath)

            except (ImportError, OSError) as e:
                logger.error("Audio library/device error: %s", e)
                self._notify_toast(f"音訊裝置錯誤：{e}", "error", 8000)
            except Exception as e:
                logger.error("Recording error: %s", e)
                self._notify_toast(f"錄音發生錯誤：{e}", "error", 8000)

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
                        self._recording_level = _calc_level(arr)
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
                    # 無系統 loopback 時，麥克風即為音量來源
                    if not sys_stream:
                        self._recording_level = _calc_level(arr)

                mic_stream = sd.InputStream(samplerate=samplerate, channels=1, callback=mic_cb)
                mic_stream.start()
                logger.info("Mic stream opened")
            except Exception as e:
                logger.warning("Cannot open mic: %s", e)

            # 兩個來源都開不起來 → 立即回報並中止，不要錄一整段空白才發現
            if not sys_stream and not mic_stream:
                logger.error("無法開啟任何音訊輸入（系統音與麥克風皆失敗）")
                self._notify_toast("無法開啟音訊裝置：系統音與麥克風皆失敗，請檢查裝置與權限", "error", 8000)
                p.terminate()
                return None

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
                self._recording_level = _calc_level(indata)

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
        # 等待存檔完成：大檔 sf.write + os.replace（尤其寫 UNC）可能要數秒～數十秒。
        # 原本只等 5s，長錄音會在檔案還沒落地就返回，清單看不到、狀態誤判。
        thread = self._recording_thread
        thread.join(timeout=120)
        finalizing = thread.is_alive()
        if finalizing:
            logger.warning("錄音存檔尚未完成（檔案較大），背景續寫中：%s", self._recording_filename)

        elapsed = time.time() - (self._recording_started_at or time.time())
        filename = self._recording_filename or ""

        self._recording_thread = None
        self._recording_stop_event = None
        self._recording_started_at = None
        self._recording_filename = None
        self._recording_level = 0.0

        # finalizing=True → 檔案仍在背景寫，前端可稍後再刷新清單
        return _ok({"filename": filename, "duration": _fmt_duration(elapsed), "finalizing": finalizing})

    def split_recording(self) -> dict:
        if not self._recording_thread or not self._recording_thread.is_alive():
            return _err(ErrorType.RECORDING_INACTIVE, "目前沒有進行中的錄音")

        # 停止目前錄音並立即開始新的
        self.stop_recording()
        return self.start_recording()

    # ── 檔案管理 ──

    def list_recordings(self) -> dict:
        recordings_dir = self._recordings_dir_path()
        if not recordings_dir.exists():
            return _ok([])

        # 交叉比對逐字稿 / 會議紀錄資料夾，推導每筆錄音的生命週期階段
        transcripts_dir = self._transcripts_dir_path()
        txt_stems = _stems_in(transcripts_dir, ".txt")
        md_stems = _stems_in(self._results_dir_path(), ".md")
        markers = _progress_markers(transcripts_dir)   # worker 寫的轉譯進度標記
        active_stem = (
            Path(self._recording_filename).stem
            if (self._recording_filename and self._recording_thread
                and self._recording_thread.is_alive())
            else None
        )

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

            stem = path.stem
            has_transcript = stem in txt_stems
            has_result = stem in md_stems
            progress = None
            if active_stem and stem == active_stem:
                status = "recording"
            elif has_result:
                status = "done"
            elif has_transcript:
                status = "summarizing"
            else:
                status = "transcribing"
                # 轉譯中才算進度：有標記 → 轉譯中(估 %)，沒標記 → 排隊中
                progress = _calc_progress(markers.get(stem))

            items.append({
                "id": path.name,
                "name": path.name,
                "stem": stem,
                "size": _fmt_size(stat.st_size),
                "duration": _wav_duration(path),
                "status": status,
                "progress": progress,
                "has_transcript": has_transcript,
                "has_result": has_result,
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
