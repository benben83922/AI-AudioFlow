"""測試點 #1/#11/#12/#13/#14：首次設定、API Token 遮罩不覆寫、資料夾驗證、Prompt、系統相依。

來源：開發計畫 §5.6 / _settings.*
"""
import pytest


def test_pipeline_info_and_toggle_auto(bridge, monkeypatch):
    """C #2：顯示目前模式 + 切換自動/手動會持久化（不真的重啟 worker）。"""
    monkeypatch.setattr("src.bridge._services._worker_running", lambda *a, **k: False)
    info = bridge.get_pipeline_info()["data"]
    assert info["mode"] == "native" and info["auto"] is False
    r = bridge.set_pipeline_auto(True)
    assert r["success"] is True and r["data"]["auto"] is True
    assert bridge._load_config()["pipeline"]["auto"] is True
    assert bridge.get_pipeline_info()["data"]["auto"] is True
    bridge.set_pipeline_auto(False)
    assert bridge._load_config()["pipeline"]["auto"] is False


def test_complete_setup_marks_done_and_saves(bridge, tmp_path, monkeypatch):
    """測試點 #1：完成首次設定 → setup_done、存錄音夾與 token；不真的拉服務。"""
    monkeypatch.setattr(bridge, "start_all_services_async", lambda: None)
    rec = tmp_path / "myrec"
    r = bridge.complete_setup(str(rec), "tok-abc")
    assert r["success"] is True
    assert bridge.setup_completed() is True
    cfg = bridge._load_config()
    assert cfg["storage"]["local_path"] == str(rec)
    assert cfg["api_keys"]["claude"] == "tok-abc"


def test_complete_setup_ignores_masked_token(bridge, monkeypatch):
    """全遮罩圓點不可當成 token 存入。"""
    monkeypatch.setattr(bridge, "start_all_services_async", lambda: None)
    bridge.complete_setup("/tmp/x", "••••")
    assert bridge._load_config()["api_keys"]["claude"] == ""


def test_get_setup_state_reflects_configured(bridge, monkeypatch):
    assert bridge.get_setup_state()["data"]["configured"] is False
    monkeypatch.setattr(bridge, "start_all_services_async", lambda: None)
    bridge.complete_setup("/tmp/x", "")
    assert bridge.get_setup_state()["data"]["configured"] is True


def test_save_api_keys_masking_does_not_overwrite(bridge):
    """測試點 #11：有效值存得進去；空字串/遮罩不覆寫既有值。"""
    bridge.save_api_keys("", "tok1")
    assert bridge._load_config()["api_keys"]["claude"] == "tok1"
    # 遮罩 → 保留
    bridge.save_api_keys("", "••••")
    assert bridge._load_config()["api_keys"]["claude"] == "tok1"
    # 空字串 → 保留
    bridge.save_api_keys("", "")
    assert bridge._load_config()["api_keys"]["claude"] == "tok1"
    # 新值 → 覆寫
    bridge.save_api_keys("", "tok2")
    assert bridge._load_config()["api_keys"]["claude"] == "tok2"


def test_save_summary_prompt_roundtrip_and_clear(bridge):
    """測試點 #13：自訂 Prompt 儲存與清除（清除＝回預設）。"""
    bridge.save_summary_prompt("  自訂內容  ")
    assert bridge.get_summary_prompt()["data"]["custom"] == "自訂內容"
    bridge.save_summary_prompt("")
    assert bridge.get_summary_prompt()["data"]["custom"] == ""
    assert bridge.get_summary_prompt()["data"]["default"]  # 內建預設非空


def test_save_storage_settings_accepts_three_distinct(bridge, tmp_path):
    """測試點 #12：三段資料夾互不相同 → 成功。"""
    r = bridge.save_storage_settings(
        str(tmp_path / "r"), str(tmp_path / "t"), str(tmp_path / "s"), "", "")
    assert r["success"] is True


def test_save_storage_settings_rejects_duplicate_and_rolls_back(bridge, tmp_path):
    """錄音與逐字稿指向同一夾 → VALIDATION,且設定回滾不留衝突。"""
    same = str(tmp_path / "dup")
    r = bridge.save_storage_settings(same, same, str(tmp_path / "s"), "", "")
    assert r["success"] is False
    assert r["error"]["type"] == "VALIDATION"
    # 回滾：local_path 應仍為原預設空字串
    assert bridge._load_config()["storage"]["local_path"] == ""


def test_get_storage_paths_structure(bridge):
    data = bridge.get_storage_paths()["data"]
    assert data["platform"] in ("windows", "wsl", "linux")
    assert data["locked"] == (data["platform"] == "windows")
    assert "recordings" in data and "transcripts" in data and "results" in data


def test_get_system_deps_structure(bridge):
    """測試點 #14：系統相依偵測回傳結構正確。"""
    data = bridge.get_system_deps()["data"]
    assert "supported" in data and "missing" in data and "can_auto" in data
    assert isinstance(data["missing"], list)
