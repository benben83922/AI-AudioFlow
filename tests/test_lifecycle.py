"""測試點 #4/#15：list_recordings 生命週期狀態推導 + get_stats 真實計數。

這是整條管線最高價值的純邏輯：由「錄音夾 / 逐字稿夾 / 會議紀錄夾」三處檔案
交叉比對推導每筆錄音的 pending → transcribing → summarizing → done 狀態。
原 spec 標為零測試的覆蓋空白，這裡補上。
來源：開發計畫 §5.6 / _recording.list_recordings
"""
from conftest import make_wav, find_item


def test_status_pending_when_only_wav(bridge, dirs):
    make_wav(dirs["rec"], "會議A")
    item = find_item(bridge.list_recordings(), "會議A")
    assert item is not None
    assert item["status"] == "pending"
    assert item["has_transcript"] is False
    assert item["has_result"] is False


def test_status_transcribing_when_request_marker(bridge, dirs):
    """已按「轉逐字稿」、寫了請求標記但還沒逐字稿 → transcribing（排隊中）。"""
    make_wav(dirs["rec"], "會議B")
    (dirs["rec"] / ".會議B.transcribe.request").write_text("1", encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議B")
    assert item["status"] == "transcribing"
    # 僅請求標記、無進度標記 → 排隊中（progress.active=False）
    assert item["progress"] == {"active": False}


def test_status_transcribing_with_progress_marker(bridge, dirs):
    """worker 寫了進度標記 → transcribing 且 progress.active=True。"""
    import json
    make_wav(dirs["rec"], "會議C")
    (dirs["tx"] / ".會議C.progress").write_text(
        json.dumps({"started": 0, "audio_seconds": 0}), encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議C")
    assert item["status"] == "transcribing"
    assert item["progress"]["active"] is True


def test_status_summarizing_when_transcript_exists(bridge, dirs):
    make_wav(dirs["rec"], "會議D")
    (dirs["tx"] / "會議D.txt").write_text("逐字稿內容", encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議D")
    assert item["status"] == "summarizing"
    assert item["has_transcript"] is True
    assert item["has_result"] is False


def test_status_done_when_result_exists(bridge, dirs):
    make_wav(dirs["rec"], "會議E")
    (dirs["tx"] / "會議E.txt").write_text("逐字稿", encoding="utf-8")
    (dirs["rs"] / "會議E.md").write_text("# 會議紀錄", encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議E")
    assert item["status"] == "done"
    assert item["has_result"] is True


def test_done_takes_priority_over_markers(bridge, dirs):
    """已有會議紀錄 → done，即使殘留請求/進度標記也不應回退。"""
    make_wav(dirs["rec"], "會議F")
    (dirs["tx"] / "會議F.txt").write_text("x", encoding="utf-8")
    (dirs["rs"] / "會議F.md").write_text("x", encoding="utf-8")
    (dirs["rec"] / ".會議F.transcribe.request").write_text("1", encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議F")
    assert item["status"] == "done"


def test_list_empty_when_no_recordings_dir(bridge):
    """錄音夾不存在 → 回空清單而非報錯。"""
    r = bridge.list_recordings()
    assert r["success"] is True
    assert r["data"] == []


def test_status_failed_when_transcribe_error(bridge, dirs):
    """轉譯失敗標記（逐字稿夾 .stem.transcribe.error）→ failed，error.stage=transcribe。"""
    import json
    make_wav(dirs["rec"], "會議G")
    (dirs["tx"] / ".會議G.transcribe.error").write_text(
        json.dumps({"stage": "transcribe", "error": "whisper 連線逾時"}), encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議G")
    assert item["status"] == "failed"
    assert item["error"]["stage"] == "transcribe"
    assert "逾時" in item["error"]["message"]


def test_status_failed_when_summary_error(bridge, dirs):
    """有逐字稿 + 整理失敗標記（會議紀錄夾 .stem.summary.error）→ failed，stage=summary。"""
    import json
    make_wav(dirs["rec"], "會議H")
    (dirs["tx"] / "會議H.txt").write_text("逐字稿", encoding="utf-8")
    (dirs["rs"] / ".會議H.summary.error").write_text(
        json.dumps({"stage": "summary", "error": "claude 未登入"}), encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議H")
    assert item["status"] == "failed"
    assert item["error"]["stage"] == "summary"


def test_request_marker_overrides_stale_transcribe_error(bridge, dirs):
    """重試轉譯：請求標記優先於殘留的轉譯錯誤標記 → transcribing（不卡在失敗）。"""
    import json
    make_wav(dirs["rec"], "會議I")
    (dirs["tx"] / ".會議I.transcribe.error").write_text(
        json.dumps({"stage": "transcribe", "error": "x"}), encoding="utf-8")
    (dirs["rec"] / ".會議I.transcribe.request").write_text("1", encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議I")
    assert item["status"] == "transcribing"


def test_done_overrides_summary_error(bridge, dirs):
    """已有會議紀錄 → done，即使殘留整理錯誤標記也不回退失敗。"""
    import json
    make_wav(dirs["rec"], "會議J")
    (dirs["tx"] / "會議J.txt").write_text("x", encoding="utf-8")
    (dirs["rs"] / "會議J.md").write_text("# 紀錄", encoding="utf-8")
    (dirs["rs"] / ".會議J.summary.error").write_text(
        json.dumps({"stage": "summary", "error": "x"}), encoding="utf-8")
    item = find_item(bridge.list_recordings(), "會議J")
    assert item["status"] == "done"


def test_get_stats_counts(bridge, dirs):
    """get_stats：今日錄音、逐字稿總數、會議紀錄總數皆為真實計數。"""
    make_wav(dirs["rec"], "S1")
    make_wav(dirs["rec"], "S2")
    (dirs["tx"] / "S1.txt").write_text("x", encoding="utf-8")
    (dirs["rs"] / "S1.md").write_text("x", encoding="utf-8")
    data = bridge.get_stats()["data"]
    assert data["recordings_today"] == 2     # 剛建立 = 今天
    assert data["transcriptions_total"] == 1
    assert data["results_total"] == 1
