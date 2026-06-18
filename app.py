"""打包入口 — 從專案根目錄編譯時的頂層進入點。

Nuitka 從專案根編譯這支，`src` 才是可被發現的頂層套件
（`from src.bridge import Bridge` 等絕對匯入才解析得到）。

`--worker` 旗標的路由在 src/main.py 的模組載入階段就會處理
（早於 import webview），因此這裡只需轉呼叫 src.main.main()。
"""

from src.main import main

if __name__ == "__main__":
    main()
