import logging
from pathlib import Path

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)


def _safe_stem(stem: str) -> str | None:
    """防路徑穿越：stem 不得含路徑分隔或上層參照。"""
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        return None
    return stem


def _count_ext(dir_, ext: str) -> int:
    if dir_ is None:
        return 0
    try:
        return sum(1 for p in Path(dir_).iterdir() if p.suffix.lower() == ext)
    except Exception:
        return 0


class ProcessingMixin:
    """成果讀取 — 逐字稿與會議紀錄的檢視 / 匯出，以及真實統計。"""

    def get_stats(self) -> dict:
        """儀表板統計：今日錄音數、已轉譯逐字稿、已生成會議紀錄（皆為真實計數）。"""
        recordings_dir = self._recordings_dir_path()

        recordings_today = 0
        if recordings_dir.exists():
            from datetime import datetime, date
            today = date.today()
            for p in recordings_dir.iterdir():
                if p.suffix.lower() in (".wav", ".mp3", ".m4a"):
                    if datetime.fromtimestamp(p.stat().st_mtime).date() == today:
                        recordings_today += 1

        return _ok({
            "recordings_today": recordings_today,
            "transcriptions_total": _count_ext(self._transcripts_dir_path(), ".txt"),
            "results_total": _count_ext(self._results_dir_path(), ".md"),
        })

    # ── 成果讀取 ──

    def get_result(self, stem: str) -> dict:
        """讀取某錄音的會議紀錄（openclaw 輸出的 .md）。"""
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        results_dir = self._results_dir_path()
        if results_dir is None:
            return _err(ErrorType.VALIDATION, "尚未設定會議紀錄輸出資料夾")
        src = Path(results_dir) / f"{stem}.md"
        if not src.exists():
            return _err(ErrorType.NOT_FOUND, "會議紀錄尚未生成")
        try:
            return _ok({"stem": stem, "content": src.read_text(encoding="utf-8")})
        except Exception as e:
            return _err(ErrorType.INTERNAL, str(e))

    def get_transcript(self, stem: str) -> dict:
        """讀取某錄音的逐字稿（STT worker 輸出的 .txt）。"""
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        transcripts_dir = self._transcripts_dir_path()
        if transcripts_dir is None:
            return _err(ErrorType.VALIDATION, "尚未設定逐字稿資料夾")
        src = Path(transcripts_dir) / f"{stem}.txt"
        if not src.exists():
            return _err(ErrorType.NOT_FOUND, "逐字稿尚未生成")
        try:
            return _ok({"stem": stem, "content": src.read_text(encoding="utf-8")})
        except Exception as e:
            return _err(ErrorType.INTERNAL, str(e))

    def export_result(self, stem: str) -> dict:
        """以系統存檔對話框把會議紀錄另存到使用者選的位置。"""
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        results_dir = self._results_dir_path()
        if results_dir is None:
            return _err(ErrorType.VALIDATION, "尚未設定會議紀錄輸出資料夾")
        src = Path(results_dir) / f"{stem}.md"
        if not src.exists():
            return _err(ErrorType.NOT_FOUND, "會議紀錄尚未生成")
        if not self._window:
            return _err(ErrorType.INTERNAL, "視窗尚未就緒")

        try:
            import webview
            content = src.read_text(encoding="utf-8")
            chosen = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"{stem}.md",
                file_types=("Markdown 檔 (*.md)", "所有檔案 (*.*)"),
            )
            if not chosen:
                return _ok({"saved": False})
            dest = chosen if isinstance(chosen, str) else chosen[0]
            Path(dest).write_text(content, encoding="utf-8")
            logger.info("會議紀錄已匯出：%s", dest)
            return _ok({"saved": True, "path": str(dest)})
        except Exception as e:
            return _err(ErrorType.INTERNAL, str(e))
