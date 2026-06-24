import os
import sys
import json
import time
import shutil
import threading
import subprocess
import logging
from datetime import datetime
from pathlib import Path

from src.bridge._helpers import _ok, _err, ErrorType
from src.bridge._platform import detect_platform, WINDOWS

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


def _error_markers(dir_, suffix: str) -> dict:
    """讀資料夾內 .<stem><suffix> 失敗標記，回傳 {stem: {error, ts, stage}}。

    worker / app 在轉譯或整理失敗時寫入；用來把該筆顯示成「失敗」並提供重試。
    """
    out: dict = {}
    if dir_ is None:
        return out
    try:
        for p in Path(dir_).iterdir():
            name = p.name
            if name.startswith(".") and name.endswith(suffix):
                stem = name[1:-len(suffix)]
                try:
                    out[stem] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    out[stem] = {"error": ""}
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

# ── PulseAudio 來源偵測（供 parec 擷取用；WSLg / 一般 Linux 桌面通用）──
# 系統音 = 預設 sink 的 .monitor（等同 Windows 的 WASAPI loopback）；麥克風 = 預設 source。
# 註：sounddevice/PortAudio 直開 monitor 在 WSLg 會卡死，故 Linux 改用 PulseAudio 原生 parec。

def _pactl(*args) -> str:
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _pulse_default_source() -> str | None:
    return _pactl("get-default-source") or None


def _pulse_monitor_source() -> str | None:
    sink = _pactl("get-default-sink")
    if sink:
        return f"{sink}.monitor"
    for line in _pactl("list", "short", "sources").splitlines():   # 後備：找任一 .monitor
        parts = line.split()
        if len(parts) >= 2 and parts[1].endswith(".monitor"):
            return parts[1]
    return None


# 列出 recordings 夾中有「轉逐字稿」請求標記的 stem（app 寫入、worker 取走轉譯）
def _request_stems(dir_) -> set[str]:
    out: set[str] = set()
    suffix = ".transcribe.request"
    try:
        for p in Path(dir_).iterdir():
            n = p.name
            if n.startswith(".") and n.endswith(suffix):
                out.add(n[1:-len(suffix)])
    except Exception:
        pass
    return out


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

    @staticmethod
    def _safe_filename_part(s: str) -> str:
        """清掉檔名不合法 / 路徑穿越字元（供會議主題、日期組檔名用）。"""
        s = (s or "").strip()
        for ch in '/\\:*?"<>|\n\r\t':
            s = s.replace(ch, "")
        return s.replace("..", "").strip()

    def _build_recording_stem(self, topic: str, date: str, recordings_dir: Path) -> str:
        """以「會議主題_日期」組檔名主幹；主題空→『錄音』、日期空→今日；碰撞加時間後綴。"""
        topic = self._safe_filename_part(topic) or "錄音"
        date = self._safe_filename_part(date) or datetime.now().strftime("%Y-%m-%d")
        base = f"{topic}_{date}"
        if (recordings_dir / f"{base}.wav").exists():        # 同主題同日再錄 → 不覆蓋
            base = f"{base}_{datetime.now().strftime('%H%M%S')}"
        return base

    def start_recording(self, topic: str = "", date: str = "") -> dict:
        if self._recording_thread and self._recording_thread.is_alive():
            return _err(ErrorType.RECORDING_ACTIVE, "錄音已在進行中")

        # 環境未就緒就不給錄（依平台/模式：Docker 或音訊裝置等）
        env = self.get_environment_status()["data"]
        if not env["can_record"]:
            return _err(ErrorType.DOCKER_UNAVAILABLE, env["message"] or "尚未就緒，無法開始錄音")

        config = self._load_config()
        local_path = config["storage"].get("local_path", "").strip()
        recordings_dir = Path(local_path) if local_path else self._recordings_dir
        recordings_dir.mkdir(parents=True, exist_ok=True)

        # 檔名 = 會議主題_日期（worker 取檔只略過 .part / 點開頭檔，自訂檔名照收）
        stem = self._build_recording_stem(topic, date, recordings_dir)
        filename = f"{stem}.wav"
        filepath = recordings_dir / filename
        # 暫存名（與最終檔同資料夾，確保 os.replace 為原子操作）
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

                plat = detect_platform()
                if plat == WINDOWS:
                    audio = _record_windows(SAMPLERATE)
                else:
                    # WSL 與純 Linux 皆走 PulseAudio parec（系統音 monitor + 麥克風混音）
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
            # 純 Linux / WSL：用 PulseAudio 原生 parec 擷取系統音（sink 的 .monitor）與
            # 麥克風並混音，對齊 Windows 行為。
            # 為何不用 sounddevice：PortAudio 直開 monitor source 在 WSLg 會卡死。
            import numpy as np

            if not shutil.which("parec"):
                logger.warning("找不到 parec，退回 sounddevice 麥克風擷取（無系統音）")
                return _record_linux_mic_sd(samplerate)

            mon = _pulse_monitor_source()
            mic = _pulse_default_source()
            if mic and mon and mic == mon:
                mic = None                       # 預設來源本身就是 monitor → 不重複擷取
            if not mon and not mic:
                logger.error("找不到可用的 PulseAudio 來源（monitor / mic）")
                self._notify_toast("找不到可用的音訊來源（PulseAudio）", "error", 8000)
                return None

            sys_chunks, mic_chunks, procs = [], [], []

            def _reader(dev, channels, chunks, is_level_src):
                cmd = ["parec", "--device", dev, "--format=float32le",
                       "--rate={}".format(samplerate), "--channels={}".format(channels)]
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                except Exception as e:
                    logger.warning("parec 啟動失敗（%s）：%s", dev, e)
                    return
                procs.append(p)
                try:
                    while not self._recording_stop_event.is_set():
                        data = p.stdout.read(8192)
                        if not data:
                            break
                        chunks.append(data)
                        if is_level_src:
                            self._recording_level = _calc_level(np.frombuffer(data, dtype="<f4"))
                finally:
                    try:
                        p.terminate()
                    except Exception:
                        pass

            threads = []
            if mon:
                t = threading.Thread(target=_reader, args=(mon, 2, sys_chunks, True),
                                     name="parec-sys", daemon=True)
                t.start(); threads.append(t)
                logger.info("System audio via parec: %s", mon)
            if mic:
                t = threading.Thread(target=_reader, args=(mic, 1, mic_chunks, not bool(mon)),
                                     name="parec-mic", daemon=True)
                t.start(); threads.append(t)
                logger.info("Mic via parec: %s", mic)

            while not self._recording_stop_event.is_set():
                self._recording_stop_event.wait(timeout=0.1)

            for p in procs:
                try:
                    p.terminate()
                except Exception:
                    pass
            for t in threads:
                t.join(timeout=2)

            def _decode(chunks, ch):
                if not chunks:
                    return None
                a = np.frombuffer(b"".join(chunks), dtype="<f4")
                if a.size == 0:
                    return None
                if ch >= 2:
                    a = a[: (a.size // 2) * 2].reshape(-1, 2)
                else:
                    a = a.reshape(-1, 1)
                    a = np.column_stack([a[:, 0], a[:, 0]])   # 單聲道 → 立體聲
                return a.astype(np.float32)

            a_sys = _decode(sys_chunks, 2)
            a_mic = _decode(mic_chunks, 1)
            if a_sys is None and a_mic is None:
                logger.warning("No audio captured (parec)")
                return None
            if a_sys is not None and a_mic is not None:
                n = max(len(a_sys), len(a_mic))
                a_sys = np.pad(a_sys, ((0, n - len(a_sys)), (0, 0)))
                a_mic = np.pad(a_mic, ((0, n - len(a_mic)), (0, 0)))
                logger.info("Mixed sys+mic (parec): %d samples", n)
                return np.clip(a_sys * 0.6 + a_mic * 0.6, -1.0, 1.0)
            return a_sys if a_sys is not None else a_mic

        def _record_linux_mic_sd(samplerate: int):
            # 後備（無 parec）：sounddevice 只錄預設麥克風，避免碰會卡死的 monitor。
            import sounddevice as sd
            import numpy as np
            frames = []

            def cb(indata, *_):
                arr = indata.copy()
                if arr.shape[1] == 1:
                    arr = np.column_stack([arr, arr])
                frames.append(arr)
                self._recording_level = _calc_level(arr)

            try:
                with sd.InputStream(samplerate=samplerate, channels=1, callback=cb):
                    while not self._recording_stop_event.is_set():
                        self._recording_stop_event.wait(timeout=0.1)
            except Exception as e:
                logger.error("sounddevice 麥克風錄音失敗：%s", e)
                self._notify_toast(f"錄音失敗：{e}", "error", 8000)
                return None
            if not frames:
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
        requested = _request_stems(recordings_dir)      # 已按「轉逐字稿」、排入佇列的 stem
        tx_errors = _error_markers(transcripts_dir, ".transcribe.error")   # 轉譯失敗
        sum_errors = _error_markers(self._results_dir_path(), ".summary.error")  # 整理失敗
        generating = getattr(self, "_generating", set())  # 正在生成會議紀錄的 stem
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
            error = None
            if active_stem and stem == active_stem:
                status = "recording"
            elif has_result:
                status = "done"
            elif has_transcript:
                if stem in generating:
                    # 正在生成會議紀錄（generating 旗標另外帶，供前端轉圈）
                    status = "summarizing"
                elif stem in sum_errors:
                    # 整理失敗 → 失敗（前端顯示錯誤 + 「重試整理」）
                    status = "failed"
                    error = {"stage": "summary",
                             "message": (sum_errors.get(stem) or {}).get("error", "")}
                else:
                    # 有逐字稿、缺會議紀錄 → 待整理（手動模式等使用者按「轉會議紀錄」）
                    status = "summarizing"
            elif stem in markers or stem in requested:
                # 已排入 / 正在轉譯：有進度標記 → 估 %，僅有請求標記 → 排隊中
                # （重試時請求標記優先於殘留錯誤標記，故先判斷此分支）
                status = "transcribing"
                progress = _calc_progress(markers.get(stem))
            elif stem in tx_errors:
                # 轉譯失敗 → 失敗（前端顯示錯誤 + 「重試轉譯」）
                status = "failed"
                error = {"stage": "transcribe",
                         "message": (tx_errors.get(stem) or {}).get("error", "")}
            else:
                # 尚無逐字稿、也未排入 → 待轉譯（手動模式等使用者按「轉逐字稿」）
                status = "pending"

            items.append({
                "id": path.name,
                "name": path.name,
                "stem": stem,
                "size": _fmt_size(stat.st_size),
                "duration": _wav_duration(path),
                "status": status,
                "progress": progress,
                "error": error,                     # 失敗時 {stage, message}，否則 None
                "has_transcript": has_transcript,
                "has_result": has_result,
                "generating": stem in generating,   # 會議紀錄生成中（供前端顯示轉圈）
                "time": time_str,
                "path": str(path),
            })

        return _ok(items)

    # ── 在系統檔案管理器開啟資料夾 / 選取檔案 ──

    def reveal_path(self, path_str: str) -> dict:
        """在系統檔案管理器開啟資料夾，或選取某檔案。Windows→explorer、WSL→explorer.exe、Linux→xdg-open。"""
        from src.bridge._platform import detect_platform, WINDOWS, WSL
        p = Path(path_str)
        if not p.exists():
            return _err(ErrorType.NOT_FOUND, f"路徑不存在：{path_str}")
        plat = detect_platform()
        try:
            if plat == WINDOWS:
                args = ["explorer.exe", str(p)] if p.is_dir() else ["explorer.exe", "/select,", str(p)]
                subprocess.Popen(args, creationflags=0x08000000)
            elif plat == WSL:
                win = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=5).stdout.strip()
                args = ["explorer.exe", win] if p.is_dir() else ["explorer.exe", "/select,", win]
                subprocess.Popen(args)
            else:
                subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
            return _ok({"message": "已開啟資料夾"})
        except Exception as e:
            return _err(ErrorType.INTERNAL, f"無法開啟資料夾：{e}")

    def open_folder(self, kind: str) -> dict:
        """開啟三段資料夾之一（recordings / transcripts / results）。"""
        getter = {"recordings": self._recordings_dir_path,
                  "transcripts": self._transcripts_dir_path,
                  "results": self._results_dir_path}.get(kind)
        if getter is None:
            return _err(ErrorType.VALIDATION, f"未知資料夾：{kind}")
        path = getter()
        if not path:
            return _err(ErrorType.VALIDATION, "資料夾尚未設定")
        p = Path(path)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return self.reveal_path(str(p))

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
