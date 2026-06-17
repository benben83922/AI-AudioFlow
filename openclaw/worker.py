"""OpenClaw 下游 worker（最小參考實作）。

職責：輪詢逐字稿資料夾 /in → 透過 OpenAI 相容 HTTP 呼叫 LLM 服務整理 → 原子化寫 /out。
只用 Python 標準庫，零第三方依賴。

注意：這是「對接合約的最小可用版」，示範整條下游怎麼動。
你完整的 OpenClaw 平台可直接取代它（compose 改 build/image 即可）；
重點是它跟 LLM 後端的邊界 = OpenAI 相容 HTTP，後端換 API 時這支不用動。
"""

import os
import time
import json
import urllib.request
from pathlib import Path

IN = Path(os.environ.get("IN_DIR", "/in"))
OUT = Path(os.environ.get("OUT_DIR", "/out"))
LLM_BASE = os.environ.get("LLM_BASE_URL", "http://llm-service:8088/v1").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "opus")
INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "600"))

SYSTEM = (
    "你是專業的中文會議記錄員。根據語音逐字稿產出一份結構完整、條理清楚的會議紀錄。"
    "只輸出 Markdown 會議紀錄本身，不要任何前言、說明或結語。"
)
INSTRUCTION = (
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


def reformat(text: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": INSTRUCTION + text},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer not-needed"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[openclaw] 監看 {IN} → {OUT}（每 {INTERVAL}s），LLM={LLM_BASE}", flush=True)
    while True:
        for f in sorted(IN.glob("*.txt")):
            base = f.stem
            out = OUT / f"{base}.md"
            tmp = OUT / f".{base}.md.part"
            if out.exists():
                continue                      # 冪等：已整理過
            try:
                print(f"[openclaw] 整理中：{base}", flush=True)
                md = reformat(f.read_text(encoding="utf-8"))
                tmp.write_text(md, encoding="utf-8")
                os.replace(tmp, out)          # 原子化落地
                print(f"[openclaw] 完成：{out.name}", flush=True)
            except Exception as e:
                print(f"[openclaw] 失敗：{base} — {e}", flush=True)
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
