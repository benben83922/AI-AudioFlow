import os
import json
import time
import shutil
import logging
import threading
from pathlib import Path

from src.bridge._helpers import _ok, _err, ErrorType

logger = logging.getLogger(__name__)

# app 直接呼叫 llm-service（不靠 openclaw）整理逐字稿用的位址與模型。
# llm-service 在 compose 綁了 127.0.0.1:8088，桌面 app 走 localhost 連得到。
DEFAULT_LLM_BASE = "http://localhost:8088/v1"
DEFAULT_LLM_MODEL = "opus"

# 會議紀錄 prompt（與 openclaw/worker.py 一致，確保有無 openclaw 產出格式相同）
_SUMMARY_SYSTEM = (
    "你是專業的中文會議記錄員。根據語音逐字稿產出一份結構完整、條理清楚的會議紀錄。"
    "只輸出 Markdown 會議紀錄本身，不要任何前言、說明或結語。"
)
_SUMMARY_INSTRUCTION = (
    "請把以下語音逐字稿整理成一份「詳細會議紀錄」，使用繁體中文 Markdown，依下列結構輸出：\n"
    "\n"
    "# 會議紀錄\n"
    "## 摘要\n"
    "（3–5 句話，總結這場會議的目的與結論）\n"
    "## 與會者\n"
    "（從逐字稿可辨識的發言人/人名列點；無法辨識則寫「未提及」）\n"
    "## 討論主題\n"
    "（依主題分節，每節用 ### 小標題，條列該主題的重點、論點與背景脈絡；"
    "保留具體數字、日期、名稱等細節）\n"
    "## 決議事項\n"
    "（條列拍板的決定；若無明確決議寫「無」）\n"
    "## 待辦事項 / 行動項\n"
    "（用核取清單，盡量標註負責人與期限：- [ ] 事項（負責人，期限）；無則寫「無」）\n"
    "## 待確認 / 未解決問題\n"
    "（條列懸而未決或需後續跟進的事項；無則寫「無」）\n"
    "\n"
    "規則：忠於逐字稿原意，修正明顯的同音字與口語贅詞；"
    "「不要杜撰」任何逐字稿中沒有的內容，無資訊的欄位明確標示「無」或「未提及」。\n\n"
    "逐字稿如下：\n\n"
)


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
    """成果讀取與生成 — 逐字稿/會議紀錄檢視、匯出、真實統計，以及直接呼叫 LLM 整理。"""

    _generating: set = set()   # 正在生成會議紀錄的 stem（含排隊中），防重複觸發
    _gen_lock = threading.Lock()   # 保護生成佇列的建立 / 啟動
    _gen_cancel: set = set()   # 已要求取消、worker 取到時跳過的排隊 stem
    _gen_active = None          # 目前實際在跑的 stem（無法中止，只能等它完成）

    # ── 失敗標記（與 worker_main 約定一致）──
    # 轉譯失敗：逐字稿夾 .<stem>.transcribe.error；整理失敗：會議紀錄夾 .<stem>.summary.error
    def _summary_error_path(self, stem: str) -> Path | None:
        results_dir = self._results_dir_path()
        return Path(results_dir) / f".{stem}.summary.error" if results_dir else None

    def _transcribe_error_path(self, stem: str) -> Path | None:
        tx_dir = self._transcripts_dir_path()
        return Path(tx_dir) / f".{stem}.transcribe.error" if tx_dir else None

    # ── 直接呼叫 LLM 整理（不靠 openclaw）──

    def _llm_base_url(self) -> str:
        url = self._load_config().get("storage", {}).get("llm_url", "").strip()
        return (url or DEFAULT_LLM_BASE).rstrip("/")

    def _summarize_native(self, transcript: str, prompt_override: str = "") -> str:
        """native 模式：直接呼叫本機 claude -p 整理（不經 llm-service 容器）。

        prompt_override 非空時用它（單筆自訂 / 試跑），否則用全域設定（空＝預設）。
        """
        import src.claude_cli as claude_cli
        from src.prompts import build_summary_prompt
        prompt = build_summary_prompt(transcript, prompt_override or self._summary_prompt())
        return claude_cli.run(prompt, model=DEFAULT_LLM_MODEL, token=self._claude_token())

    def _summarize_via_llm(self, transcript: str, prompt_override: str = "") -> str:
        """整理逐字稿為會議紀錄 Markdown。native 走本機 claude -p；docker 走 llm-service HTTP。

        prompt_override 非空時優先採用（單筆自訂 prompt / 預覽試跑）。
        """
        if self._pipeline_mode() == "native":
            return self._summarize_native(transcript, prompt_override)
        from src.prompts import build_summary_prompt
        import httpx
        # 自訂 prompt（含逐字稿）整段當 user 訊息送（llm-service 後端 = claude -p）
        body = {
            "model": DEFAULT_LLM_MODEL,
            "messages": [
                {"role": "user", "content": build_summary_prompt(transcript, prompt_override or self._summary_prompt())},
            ],
        }
        with httpx.Client(timeout=600) as client:
            resp = client.post(f"{self._llm_base_url()}/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def transcribe_recording(self, filename: str) -> dict:
        """手動觸發：把某錄音排入轉譯（寫「轉逐字稿」請求標記，由處理 worker 取走）。

        worker 在手動模式下只處理帶此標記的檔；產出逐字稿後標記自動消費。清單會輪詢
        刷新，完成後該筆自動進到「待整理」（可再按「轉會議紀錄」）。
        """
        recordings_dir = self._recordings_dir_path()
        target = (recordings_dir / filename).resolve()
        if not str(target).startswith(str(recordings_dir.resolve())):
            return _err(ErrorType.VALIDATION, "非法的檔案路徑")
        if not target.exists():
            return _err(ErrorType.NOT_FOUND, f"找不到錄音：{filename}")

        stem = target.stem
        # 正在錄這個檔 → 不可轉
        if (self._recording_filename and Path(self._recording_filename).stem == stem
                and self._recording_thread and self._recording_thread.is_alive()):
            return _err(ErrorType.VALIDATION, "此檔正在錄音中")
        # 已有逐字稿 → 免再轉
        transcripts_dir = self._transcripts_dir_path()
        if transcripts_dir and (Path(transcripts_dir) / f"{stem}.txt").exists():
            return _err(ErrorType.VALIDATION, "逐字稿已存在")

        # 處理 worker 需在跑，且轉譯引擎需就緒（否則標記寫了也轉不動，先給清楚回饋）
        from src.bridge._worker import _worker_running
        if not _worker_running():
            return _err(ErrorType.VALIDATION, "處理服務未啟動，請先於服務頁啟動")
        if self._pipeline_mode() == "native":
            from src.bridge._docker import _native_engine_ready
            if not _native_engine_ready():
                return _err(ErrorType.VALIDATION, "尚未安裝 faster-whisper，無法轉譯")
        elif self._container_state() != "running":
            return _err(ErrorType.DOCKER_UNAVAILABLE, "Whisper 引擎未啟動，請先於服務頁啟動")

        # 重試入口：清掉舊的轉譯失敗標記，狀態立即脫離「失敗」回到排隊/轉譯中
        tx_err = self._transcribe_error_path(stem)
        if tx_err is not None:
            try:
                tx_err.unlink(missing_ok=True)
            except Exception:
                pass

        try:
            (recordings_dir / f".{stem}.transcribe.request").write_text("1", encoding="utf-8")
        except Exception as e:
            return _err(ErrorType.INTERNAL, f"無法建立轉譯請求：{e}")
        logger.info("已排入轉譯：%s", stem)
        return _ok({"message": "已排入轉譯，稍候自動更新"})

    def _summary_engine_ready(self) -> tuple[bool, str]:
        """整理引擎是否就緒（native：claude CLI；docker：已填 Claude token）。"""
        if self._pipeline_mode() == "native":
            import src.claude_cli as claude_cli
            if not claude_cli.available():
                return False, "未偵測到 Claude CLI，無法生成會議紀錄（請安裝 claude 或於 WSL 登入）"
        elif not self._claude_token():
            return False, "尚未填入 Claude 訂閱 Token，無法生成會議紀錄"
        return True, ""

    def _backup_result(self, out: Path, stem: str) -> None:
        """覆蓋既有會議紀錄前，把舊檔備份到 results/.versions/{stem}.{時間}.md（可還原）。"""
        try:
            vdir = out.parent / ".versions"
            vdir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(out, vdir / f"{stem}.{stamp}.md")
            logger.info("已備份舊會議紀錄：%s", vdir / f"{stem}.{stamp}.md")
        except Exception as e:
            logger.warning("備份舊會議紀錄失敗（%s）：%s", stem, e)

    def _start_generate(self, stem: str, *, overwrite: bool, prompt_override: str = "") -> dict:
        """生成會議紀錄的共用核心。overwrite=True 允許覆蓋既有（重新生成，覆蓋前備份）。

        prompt_override 非空＝這次用自訂 prompt（單筆覆寫 / 試跑採用）。背景執行，完成後
        前端輪詢刷新；失敗只在「沒有既有紀錄可保留」時才寫失敗標記（避免覆蓋失敗污染 done）。
        """
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        ok, msg = self._summary_engine_ready()
        if not ok:
            return _err(ErrorType.VALIDATION, msg)

        transcripts_dir = self._transcripts_dir_path()
        results_dir = self._results_dir_path()
        if transcripts_dir is None or results_dir is None:
            return _err(ErrorType.VALIDATION, "逐字稿 / 會議紀錄資料夾尚未就緒")

        txt = Path(transcripts_dir) / f"{stem}.txt"
        if not txt.exists():
            return _err(ErrorType.NOT_FOUND, "逐字稿尚未生成")
        out = Path(results_dir) / f"{stem}.md"
        if out.exists() and not overwrite:
            return _err(ErrorType.VALIDATION, "會議紀錄已存在")
        if stem in self._generating:
            return _err(ErrorType.VALIDATION, "此筆正在生成中…")

        err_path = self._summary_error_path(stem)
        if err_path is not None:                 # 重試/重生入口：先清舊的整理失敗標記
            try:
                err_path.unlink(missing_ok=True)
            except Exception:
                pass

        # 排入序列化佇列（單一 worker 逐筆生成）——批次整理時不會同時觸發 N 個 LLM 呼叫。
        # 排隊中的 stem 也算在 _generating，清單會顯示「整理中…」（即佇列視圖）。
        self._enqueue_generate(stem, overwrite, prompt_override or "")
        return _ok({"message": "重新生成會議紀錄…" if overwrite else "正在生成會議紀錄…"})

    # ── 生成佇列（序列化，避免批次整理同時打爆 LLM）──

    def _enqueue_generate(self, stem: str, overwrite: bool, prompt_override: str) -> None:
        import queue as _queue
        with ProcessingMixin._gen_lock:
            q = getattr(self, "_gen_queue", None)
            if q is None:
                q = self._gen_queue = _queue.Queue()
            self._generating.add(stem)           # 即時反映（含排隊中），清單顯示「整理中…」
            q.put((stem, overwrite, prompt_override))
            t = getattr(self, "_gen_thread", None)
            if t is None or not t.is_alive():
                self._gen_thread = threading.Thread(
                    target=self._gen_loop, name="gen-worker", daemon=True)
                self._gen_thread.start()

    def _gen_loop(self) -> None:
        import queue as _queue
        q = self._gen_queue
        while True:
            try:
                stem, overwrite, override = q.get(timeout=2)
            except _queue.Empty:
                return                            # 閒置即退出；下次 enqueue 會重啟
            # 排隊中被取消 → 跳過不生成
            if stem in self._gen_cancel:
                self._gen_cancel.discard(stem)
                self._generating.discard(stem)
                q.task_done()
                continue
            type(self)._gen_active = stem         # 標記「實際在跑」（此筆無法中止）
            try:
                self._do_generate(stem, overwrite, override)
            finally:
                type(self)._gen_active = None
                self._generating.discard(stem)
                q.task_done()

    def cancel_generate(self, stem: str) -> dict:
        """取消整理：排隊中的可立即取消；已實際在跑的無法中止（需等它完成）。"""
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        if stem not in self._generating:
            return _err(ErrorType.VALIDATION, "此筆未在整理佇列中")
        if self._gen_active == stem:
            return _err(ErrorType.VALIDATION, "此筆正在整理中，無法中止（需等它完成）")
        self._gen_cancel.add(stem)
        self._generating.discard(stem)           # 立即從清單移除「整理中」
        logger.info("已取消排隊中的整理：%s", stem)
        return _ok({"message": "已取消排隊中的整理"})

    def cancel_transcribe(self, filename: str) -> dict:
        """取消轉譯：排隊中（僅有請求標記）的可立即取消；已在轉譯（有進度標記）的無法中止。"""
        recordings_dir = self._recordings_dir_path()
        target = (recordings_dir / filename).resolve()
        if not str(target).startswith(str(recordings_dir.resolve())):
            return _err(ErrorType.VALIDATION, "非法的檔案路徑")
        stem = target.stem
        transcripts_dir = self._transcripts_dir_path()
        prog = Path(transcripts_dir) / f".{stem}.progress" if transcripts_dir else None
        if prog is not None and prog.exists():
            return _err(ErrorType.VALIDATION, "此筆正在轉譯中，無法中止（需等它完成）")
        req = recordings_dir / f".{stem}.transcribe.request"
        if req.exists():
            try:
                req.unlink(missing_ok=True)
            except Exception as e:
                return _err(ErrorType.INTERNAL, f"取消失敗：{e}")
            logger.info("已取消排隊中的轉譯：%s", stem)
            return _ok({"message": "已取消排隊中的轉譯"})
        return _err(ErrorType.VALIDATION, "此筆未在轉譯佇列中")

    def _do_generate(self, stem: str, overwrite: bool, prompt_override: str) -> None:
        """實際生成一筆會議紀錄（由佇列 worker 逐筆呼叫）。"""
        transcripts_dir = self._transcripts_dir_path()
        results_dir = self._results_dir_path()
        if transcripts_dir is None or results_dir is None:
            return
        txt = Path(transcripts_dir) / f"{stem}.txt"
        out = Path(results_dir) / f"{stem}.md"
        err_path = self._summary_error_path(stem)
        if not txt.exists():
            return
        try:
            md = self._summarize_via_llm(txt.read_text(encoding="utf-8"), prompt_override or "")
            Path(results_dir).mkdir(parents=True, exist_ok=True)
            if out.exists():
                self._backup_result(out, stem)   # 覆蓋前備份舊版
            tmp = out.with_name(f".{out.name}.part")
            tmp.write_text(md, encoding="utf-8")
            os.replace(tmp, out)                  # 原子化落地
            logger.info("會議紀錄已生成：%s", out)
            self._notify_toast(f"會議紀錄已生成：{stem}", "success", 5000)
        except Exception as e:
            logger.error("生成會議紀錄失敗（%s）：%s", stem, e)
            # 失敗留痕：僅在沒有既有紀錄可退守時才標記「失敗」（覆蓋失敗保留舊紀錄、不污染 done）
            if err_path is not None and not out.exists():
                try:
                    err_path.write_text(
                        json.dumps({"stage": "summary", "error": str(e)[:2000],
                                    "ts": time.time()}, ensure_ascii=False),
                        encoding="utf-8")
                except Exception:
                    pass
            self._notify_toast(f"生成會議紀錄失敗：{e}", "error", 8000)

    # ── 批次操作 ──

    def transcribe_batch(self, filenames) -> dict:
        """批次排入轉譯：逐檔走 transcribe_recording（worker 端本就序列處理）。"""
        if not isinstance(filenames, list):
            return _err(ErrorType.VALIDATION, "參數需為清單")
        queued, skipped, first_err = 0, 0, ""
        for fn in filenames:
            r = self.transcribe_recording(fn)
            if r.get("success"):
                queued += 1
            else:
                skipped += 1
                first_err = first_err or (r.get("error") or {}).get("message", "")
        msg = f"已排入轉譯 {queued} 筆" + (f"，略過 {skipped} 筆" if skipped else "")
        return _ok({"queued": queued, "skipped": skipped, "message": msg, "detail": first_err})

    def generate_batch(self, stems) -> dict:
        """批次排入整理（僅尚無會議紀錄者）；序列化佇列逐筆生成，不會同時打爆 LLM。"""
        if not isinstance(stems, list):
            return _err(ErrorType.VALIDATION, "參數需為清單")
        queued, skipped, first_err = 0, 0, ""
        for s in stems:
            r = self._start_generate(s, overwrite=False)
            if r.get("success"):
                queued += 1
            else:
                skipped += 1
                first_err = first_err or (r.get("error") or {}).get("message", "")
        msg = f"已排入整理 {queued} 筆" + (f"，略過 {skipped} 筆" if skipped else "")
        return _ok({"queued": queued, "skipped": skipped, "message": msg, "detail": first_err})

    def generate_result(self, stem: str, prompt_override: str = "") -> dict:
        """手動把某逐字稿餵 LLM 生成會議紀錄（.md）。背景執行，完成後清單自動刷新顯示。"""
        return self._start_generate(stem, overwrite=False, prompt_override=prompt_override)

    def regenerate_result(self, stem: str, prompt_override: str = "") -> dict:
        """重新生成已存在的會議紀錄（覆蓋，覆蓋前自動備份到 .versions/）。"""
        return self._start_generate(stem, overwrite=True, prompt_override=prompt_override)

    def preview_result(self, stem: str, prompt_override: str = "") -> dict:
        """試跑：用（可選）自訂 prompt 生成會議紀錄並回傳內容，但「不寫檔」。

        同步呼叫 LLM，可能需數十秒；供「整理設定」modal 預覽用。
        """
        stem = _safe_stem(stem)
        if stem is None:
            return _err(ErrorType.VALIDATION, "非法的檔名")
        ok, msg = self._summary_engine_ready()
        if not ok:
            return _err(ErrorType.VALIDATION, msg)
        transcripts_dir = self._transcripts_dir_path()
        if transcripts_dir is None:
            return _err(ErrorType.VALIDATION, "尚未設定逐字稿資料夾")
        txt = Path(transcripts_dir) / f"{stem}.txt"
        if not txt.exists():
            return _err(ErrorType.NOT_FOUND, "逐字稿尚未生成")
        try:
            md = self._summarize_via_llm(txt.read_text(encoding="utf-8"), prompt_override or "")
            return _ok({"stem": stem, "content": md})
        except Exception as e:
            logger.error("預覽生成失敗（%s）：%s", stem, e)
            return _err(ErrorType.INTERNAL, f"預覽生成失敗：{e}")

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
