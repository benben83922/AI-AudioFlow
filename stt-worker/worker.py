#!/usr/bin/env python3
"""STT worker 薄殼 — 實作已移到套件 `src/worker_main.py`（唯一真相）。

保留這支是為了「在 WSL 獨立啟動 worker」的相容用法：
    python stt-worker/worker.py
app 與打包後的 exe 則改走 `--worker` 內嵌模式，不會用到這支。
"""

import sys
from pathlib import Path

# 把專案根目錄加入 import 路徑，讓獨立執行也能找到 src 套件
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.worker_main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
