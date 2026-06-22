"""會議紀錄整理用的 prompt（純 Windows 版）。

與原系統 openclaw / llm-service 使用的 prompt 完全一致，確保產出格式相同。
純 Windows 版不再經過 llm-service 的 OpenAI 相容外殼，system 與指令直接併入
單一 prompt 由 stdin 餵給 `claude -p`（避開跨界引號 / 編碼問題）。
"""

from __future__ import annotations

SUMMARY_SYSTEM = (
    "你是專業的中文會議記錄員。根據語音逐字稿產出一份結構完整、條理清楚的會議紀錄。"
    "只輸出 Markdown 會議紀錄本身，不要任何前言、說明或結語。"
)

SUMMARY_INSTRUCTION = (
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


def build_summary_prompt(transcript: str) -> str:
    """把 system + 指令 + 逐字稿併成單一 prompt（走 stdin 餵 claude -p）。"""
    return f"{SUMMARY_SYSTEM}\n\n{SUMMARY_INSTRUCTION}{transcript}"
