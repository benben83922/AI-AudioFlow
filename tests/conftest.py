"""整合測試共用 fixture。

直接建構 Bridge（pywebview 桌面 app 的後端 js_api），以臨時 project_root 隔離
真實設定與檔案。強制 pipeline.mode=native，讓逐字稿/會議紀錄資料夾解析為
data_root/transcripts、data_root/results（不觸發 WSL 偵測，結果可決定性）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bridge import Bridge  # noqa: E402


@pytest.fixture
def bridge(tmp_path):
    """乾淨的 Bridge：native 模式、手動管線、臨時工作目錄。"""
    b = Bridge(project_root=str(tmp_path))
    cfg = b._load_config()
    cfg["pipeline"]["mode"] = "native"   # 路徑解析走本機 transcripts/ results/
    cfg["pipeline"]["auto"] = False
    b._save_config(cfg)
    return b


@pytest.fixture
def dirs(bridge):
    """解析並建立三段資料夾，回傳 Path 方便種測試資料。"""
    rec = bridge._recordings_dir_path()
    tx = Path(bridge._transcripts_dir_path())
    rs = Path(bridge._results_dir_path())
    for d in (rec, tx, rs):
        d.mkdir(parents=True, exist_ok=True)
    return {"rec": rec, "tx": tx, "rs": rs}


def make_wav(d: Path, stem: str) -> Path:
    """種一個錄音檔（內容不重要，狀態邏輯只看副檔名與是否存在）。"""
    p = d / f"{stem}.wav"
    p.write_bytes(b"RIFF....WAVE")
    return p


def find_item(result: dict, stem: str) -> dict | None:
    for it in result["data"]:
        if it["stem"] == stem:
            return it
    return None
