"""測試點 #2/#8/#9：錄音控制守衛、檔名組裝、刪除（含路徑穿越防護）。

不真正擷取音訊（需音訊裝置）；驗證的是控制流守衛與檔案管理這些可決定性邏輯。
來源：開發計畫 §5.1 / _recording.*
"""
from datetime import datetime

from conftest import make_wav


def test_stop_recording_when_inactive(bridge):
    """測試點 #2:沒有進行中的錄音時停止 → RECORDING_INACTIVE。"""
    r = bridge.stop_recording()
    assert r["success"] is False and r["error"]["type"] == "RECORDING_INACTIVE"


def test_split_recording_when_inactive(bridge):
    """測試點 #9:沒在錄音時切割 → RECORDING_INACTIVE。"""
    r = bridge.split_recording()
    assert r["success"] is False and r["error"]["type"] == "RECORDING_INACTIVE"


def test_recording_status_idle(bridge):
    data = bridge.get_recording_status()["data"]
    assert data["recording"] is False
    assert data["level"] == 0.0


def test_safe_filename_part_strips_illegal(bridge):
    fn = bridge._safe_filename_part('a/b\\c:d*e?"f<g>h|i\n..j')
    for bad in '/\\:*?"<>|\n':
        assert bad not in fn
    assert ".." not in fn


def test_build_stem_defaults(bridge):
    stem = bridge._build_recording_stem("", "", bridge._recordings_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    assert stem == f"錄音_{today}"


def test_build_stem_collision_adds_suffix(bridge, dirs):
    make_wav(dirs["rec"], "題_2026-01-01")
    stem = bridge._build_recording_stem("題", "2026-01-01", dirs["rec"])
    assert stem != "題_2026-01-01"          # 碰撞 → 加時間後綴避免覆蓋
    assert stem.startswith("題_2026-01-01_")


# ── delete_recording（測試點 #8）──

def test_delete_recording_success(bridge, dirs):
    p = make_wav(dirs["rec"], "刪我")
    r = bridge.delete_recording("刪我.wav")
    assert r["success"] is True
    assert not p.exists()


def test_delete_recording_not_found(bridge, dirs):
    r = bridge.delete_recording("不存在.wav")
    assert r["success"] is False and r["error"]["type"] == "NOT_FOUND"


def test_delete_recording_rejects_path_traversal(bridge, dirs):
    r = bridge.delete_recording("../../etc/passwd")
    assert r["success"] is False and r["error"]["type"] == "VALIDATION"
