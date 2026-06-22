"""成果讀取與生成（純 Windows 版）。

「整理」不再經過 llm-service / openclaw，改由 src.claude_cli 直接呼叫 `claude -p`
（雙偵測：Windows 原生優先、WSL 後備）。逐字稿 / 會議紀錄檢視、匯出、統計不變。
"""

import os
import logging
import threading
from pathlib import Path

import src.claude_cli as claude_cli
from src.prompts import build_summary_prompt
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
    """成果讀取與生成 — 逐字稿/會議紀錄檢視、匯出、真實統計，以及直接呼叫 claude 整理。"""

    _generating: set = set()   # 正在生成會議紀錄的 stem，防重複觸發

    # ── 直接呼叫 claude 整理（不靠 openclaw / llm-service）──

    def _llm_model(self) -> str:
        return self._load_config().get("llm", {}).get("model", "").strip() or "opus"

    def generate_result(self, stem: str) -> dict:
        """手動把某逐字稿餵 claude -p 生成會議紀錄（.md）。背景執行，完成後清單自動刷新。"""
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        if not claude_cli.available():
            return _err(ErrorType.CLAUDE_UNAVAILABLE,
                        "找不到 Claude CLI（Windows / WSL 皆未偵測到），無法生成會議紀錄")

        transcripts_dir = self._transcripts_dir_path()
        results_dir = self._results_dir_path()

        txt = Path(transcripts_dir) / f"{stem}.txt"
        if not txt.exists():
            return _err(ErrorType.NOT_FOUND, "逐字稿尚未生成")
        out = Path(results_dir) / f"{stem}.md"
        if out.exists():
            return _err(ErrorType.VALIDATION, "會議紀錄已存在")
        if stem in self._generating:
            return _err(ErrorType.VALIDATION, "此筆正在生成中…")

        self._generating.add(stem)

        def _run():
            try:
                content = txt.read_text(encoding="utf-8")
                md = claude_cli.run(
                    build_summary_prompt(content),
                    model=self._llm_model(),
                    token=self._claude_token(),
                )
                Path(results_dir).mkdir(parents=True, exist_ok=True)
                tmp = out.with_name(f".{out.name}.part")
                tmp.write_text(md, encoding="utf-8")
                os.replace(tmp, out)          # 原子化落地
                logger.info("會議紀錄已生成：%s", out)
                self._notify_toast(f"會議紀錄已生成：{stem}", "success", 5000)
            except Exception as e:
                logger.error("生成會議紀錄失敗（%s）：%s", stem, e)
                self._notify_toast(f"生成會議紀錄失敗：{e}", "error", 8000)
            finally:
                self._generating.discard(stem)

        threading.Thread(target=_run, name=f"gen-result-{stem}", daemon=True).start()
        return _ok({"message": "正在生成會議紀錄…"})

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
        """讀取某錄音的會議紀錄（.md）。"""
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        src = Path(self._results_dir_path()) / f"{stem}.md"
        if not src.exists():
            return _err(ErrorType.NOT_FOUND, "會議紀錄尚未生成")
        try:
            return _ok({"stem": stem, "content": src.read_text(encoding="utf-8")})
        except Exception as e:
            return _err(ErrorType.INTERNAL, str(e))

    def get_transcript(self, stem: str) -> dict:
        """讀取某錄音的逐字稿（.txt）。"""
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        src = Path(self._transcripts_dir_path()) / f"{stem}.txt"
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
        src = Path(self._results_dir_path()) / f"{stem}.md"
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
