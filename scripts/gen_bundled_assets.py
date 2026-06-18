#!/usr/bin/env python3
"""打包前置：把 docker compose 相關檔（compose 檔 + 兩個 build context）
打包成 src/_bundled_assets.py（base64 內嵌）。

這樣 Nuitka 會把它編進單一 exe，app 啟動時再自動寫到工作資料夾，
讓 `docker compose` 有實體檔可讀 —— 使用者只需拿到一個 exe。

用法（build.bat 會自動呼叫）：python scripts/gen_bundled_assets.py
"""

import base64
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 需要內嵌的檔（相對專案根；= 發佈時原本要跟 exe 一起給的那些）
FILES = [
    "docker-compose.yml",
    "llm-service/Dockerfile",
    "llm-service/server.js",
    "openclaw/Dockerfile",
    "openclaw/worker.py",
]


def main() -> int:
    lines = [
        '"""自動產生，請勿手改。由 scripts/gen_bundled_assets.py 生成。',
        "",
        "compose 內容以 base64 內嵌；app 啟動（打包模式）時寫到工作資料夾。",
        '"""',
        "",
        "ASSETS = {",
    ]
    for rel in FILES:
        path = ROOT / rel
        if not path.exists():
            raise SystemExit(f"找不到要內嵌的檔：{rel}")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        lines.append(f"    {rel!r}: {b64!r},")
    lines.append("}")

    out = ROOT / "src" / "_bundled_assets.py"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已產生 {out}（內嵌 {len(FILES)} 個檔）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
