"""faster-whisper 原生轉譯引擎（in-process）。

取代原系統的 whisper 容器（hwdsl2/whisper-server）：模型在本機 Python 行程內
載入，直接吃音檔回傳逐字稿文字，全程不經 HTTP、不需 Docker / WSL。

- 模型延遲載入並以 (model, device, compute_type) 為鍵快取；同設定只載一次。
- `download_root` 可指向「隨包附帶」的模型資料夾，達成「執行時不下載」；
  未預先放好時 faster-whisper 會在首次使用自動下載到該目錄。
- CPU：device=cpu / compute_type=int8（預設，無 GPU 依賴）。
  有 NVIDIA GPU：device=cuda / compute_type=float16（需 CUDA/cuDNN runtime）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger("stt-engine")

_model = None
_model_key: tuple | None = None
_lock = threading.Lock()


def _load(model: str, device: str, compute_type: str, download_root: str | None):
    """載入（或取快取）WhisperModel。重依賴在此才 import，避免 GUI 行程被牽連。"""
    global _model, _model_key
    key = (model, device, compute_type)
    with _lock:
        if _model is not None and _model_key == key:
            return _model
        from faster_whisper import WhisperModel

        logger.info("載入 whisper 模型 %s（device=%s compute=%s）…", model, device, compute_type)
        kwargs: dict = {}
        if download_root:
            Path(download_root).mkdir(parents=True, exist_ok=True)
            kwargs["download_root"] = download_root
        _model = WhisperModel(model, device=device, compute_type=compute_type, **kwargs)
        _model_key = key
        logger.info("模型載入完成")
        return _model


def transcribe(
    audio_path,
    *,
    model: str = "large-v3-turbo",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
    download_root: str | None = None,
) -> str:
    """轉譯單一音檔，回傳逐字稿純文字。"""
    m = _load(model, device, compute_type, download_root)
    segments, _info = m.transcribe(str(audio_path), language=language)
    return "".join(seg.text for seg in segments).strip()
