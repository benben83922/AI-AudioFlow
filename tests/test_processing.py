"""測試點 #5/#6/#7：轉逐字稿與會議紀錄的觸發前置驗證、成果讀取、路徑穿越防護。

不真正呼叫 whisper / claude（那需外部引擎與網路）；驗證的是「按鈕按下去之前」
Bridge 的把關邏輯與檔案讀取，這些都是可決定性的真實後端行為。
來源：開發計畫 §5.2/§5.7 / _processing.*
"""
import pytest

from conftest import make_wav


# ── get_transcript / get_result 讀取與防護 ──

def test_get_transcript_rejects_path_traversal(bridge):
    r = bridge.get_transcript("../secret")
    assert r["success"] is False and r["error"]["type"] == "VALIDATION"


def test_get_transcript_not_found(bridge, dirs):
    r = bridge.get_transcript("不存在")
    assert r["success"] is False and r["error"]["type"] == "NOT_FOUND"


def test_get_transcript_reads_content(bridge, dirs):
    (dirs["tx"] / "G.txt").write_text("逐字稿內容", encoding="utf-8")
    r = bridge.get_transcript("G")
    assert r["success"] is True
    assert r["data"]["content"] == "逐字稿內容"


def test_get_result_not_found(bridge, dirs):
    r = bridge.get_result("無")
    assert r["success"] is False and r["error"]["type"] == "NOT_FOUND"


def test_get_result_reads_content(bridge, dirs):
    (dirs["rs"] / "H.md").write_text("# 會議紀錄", encoding="utf-8")
    r = bridge.get_result("H")
    assert r["success"] is True and r["data"]["content"] == "# 會議紀錄"


# ── 取消進行中工作（#20）──

def test_cancel_transcribe_removes_queued_request(bridge, dirs):
    """排隊中（僅請求標記、無進度標記）→ 可取消，標記被刪。"""
    make_wav(dirs["rec"], "排隊")
    req = dirs["rec"] / ".排隊.transcribe.request"
    req.write_text("1", encoding="utf-8")
    r = bridge.cancel_transcribe("排隊.wav")
    assert r["success"] is True
    assert not req.exists()


def test_cancel_transcribe_rejects_when_active(bridge, dirs):
    """已在轉譯（有進度標記）→ 無法中止。"""
    make_wav(dirs["rec"], "轉中")
    (dirs["rec"] / ".轉中.transcribe.request").write_text("1", encoding="utf-8")
    (dirs["tx"] / ".轉中.progress").write_text("{}", encoding="utf-8")
    r = bridge.cancel_transcribe("轉中.wav")
    assert r["success"] is False
    assert "無法中止" in r["error"]["message"]


def test_cancel_transcribe_rejects_when_not_queued(bridge, dirs):
    make_wav(dirs["rec"], "未排")
    r = bridge.cancel_transcribe("未排.wav")
    assert r["success"] is False
    assert "未在轉譯佇列" in r["error"]["message"]


def test_cancel_generate_rejects_when_not_queued(bridge, dirs):
    r = bridge.cancel_generate("沒在跑")
    assert r["success"] is False
    assert "未在整理佇列" in r["error"]["message"]


def test_cancel_generate_cancels_queued(bridge, dirs):
    """排隊中（在 _generating、非 active）→ 可取消，從 _generating 移除。"""
    try:
        bridge._generating.add("排隊整理")
        bridge.__class__._gen_active = None
        r = bridge.cancel_generate("排隊整理")
        assert r["success"] is True
        assert "排隊整理" not in bridge._generating
    finally:
        bridge._generating.discard("排隊整理")
        bridge.__class__._gen_cancel.discard("排隊整理")


# ── transcribe_recording 前置驗證（測試點 #5）──

def test_transcribe_not_found(bridge, dirs):
    r = bridge.transcribe_recording("沒這檔.wav")
    assert r["success"] is False and r["error"]["type"] == "NOT_FOUND"


def test_transcribe_rejects_path_traversal(bridge, dirs):
    r = bridge.transcribe_recording("../evil.wav")
    assert r["success"] is False and r["error"]["type"] == "VALIDATION"


def test_transcribe_rejects_when_transcript_exists(bridge, dirs):
    """已有逐字稿 → 不重轉（此檢查早於 worker 狀態檢查）。"""
    make_wav(dirs["rec"], "已轉")
    (dirs["tx"] / "已轉.txt").write_text("x", encoding="utf-8")
    r = bridge.transcribe_recording("已轉.wav")
    assert r["success"] is False
    assert "逐字稿已存在" in r["error"]["message"]


def test_transcribe_rejects_when_worker_down(bridge, dirs, monkeypatch):
    """worker 未跑 → 擋下並提示先啟動服務。"""
    monkeypatch.setattr("src.bridge._worker._worker_running", lambda *a, **k: False)
    make_wav(dirs["rec"], "待轉")
    r = bridge.transcribe_recording("待轉.wav")
    assert r["success"] is False
    assert "處理服務未啟動" in r["error"]["message"]


# ── generate_result 前置驗證（測試點 #6）──

def test_generate_result_rejects_illegal_stem(bridge):
    r = bridge.generate_result("../x")
    assert r["success"] is False and r["error"]["type"] == "VALIDATION"


def test_generate_result_transcript_missing(bridge, dirs, monkeypatch):
    """claude 可用但逐字稿不存在 → NOT_FOUND（不會起背景生成緒）。"""
    monkeypatch.setattr("src.claude_cli.available", lambda: True)
    r = bridge.generate_result("沒逐字稿")
    assert r["success"] is False and r["error"]["type"] == "NOT_FOUND"


def test_generate_result_rejects_when_result_exists(bridge, dirs, monkeypatch):
    """已有會議紀錄 → 不重生成。"""
    monkeypatch.setattr("src.claude_cli.available", lambda: True)
    (dirs["tx"] / "重複.txt").write_text("x", encoding="utf-8")
    (dirs["rs"] / "重複.md").write_text("x", encoding="utf-8")
    r = bridge.generate_result("重複")
    assert r["success"] is False
    assert "會議紀錄已存在" in r["error"]["message"]


def test_generate_result_rejects_without_claude(bridge, dirs, monkeypatch):
    """native 模式未偵測到 claude CLI → 擋下並提示安裝/登入。"""
    monkeypatch.setattr("src.claude_cli.available", lambda: False)
    (dirs["tx"] / "X.txt").write_text("x", encoding="utf-8")
    r = bridge.generate_result("X")
    assert r["success"] is False
    assert "Claude CLI" in r["error"]["message"]
