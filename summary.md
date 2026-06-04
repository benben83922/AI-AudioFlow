# AI AudioFlow 系統架構

## 概覽

本系統將麥克風與喇叭音訊自動錄製、轉譯、摘要，並分發至日曆、通訊平台與筆記工具。

---

## 流程圖

```mermaid
graph TD
    %% 階段一：音訊擷取
    subgraph Phase_1 [第一階段：音訊擷取 & 本地儲存]
        A[麥克風輸入] -->|硬體/虛擬音訊路由| C(系統混音裝置: VoiceMeeter / BlackHole)
        B[系統喇叭輸出] -->|硬體/虛擬音訊路由| C
        C -->|音訊流| D[Python 錄音腳本 sounddevice / PyAudio]
        D -->|定時切割 / 壓縮成 MP3| E[指定本地資料夾]
    end

    %% 階段二：AI 轉譯與處理
    subgraph Phase_2 [第二階段：AI 轉譯 & 結構化摘要]
        E -->|偵測新檔案 / 觸發上傳| F[雲端儲存空間 Google Drive / Dropbox]
        F -->|Webhook 觸發| G[自動化整合平台 Make / Zapier]
        G -->|1. 傳送音訊| H[OpenAI Whisper API 語音轉文字]
        H -->|2. 回傳逐字稿文字| G
        G -->|3. 傳送逐字稿 + Prompt| I[OpenClaw / Claude API 結構化摘要]
        I -->|4. 回傳 Markdown 摘要| G
    end

    %% 階段三：分發與儲存
    subgraph Phase_3 [第三階段：自動化分發]
        G -->|建立行程與內文| J[Google 日曆]
        G -->|發送即時通知| K[Discord / Slack / LINE]
        G -->|備份 Markdown 筆記| L[本地資料夾 / Obsidian / Notion]
    end

    style Phase_1 fill:#f9f,stroke:#333,stroke-width:2px
    style Phase_2 fill:#bbf,stroke:#333,stroke-width:2px
    style Phase_3 fill:#bfb,stroke:#333,stroke-width:2px
```

---

## 第一階段：音訊擷取 & 本地儲存

**目標**：同時捕捉麥克風輸入與系統喇叭輸出，合併後儲存至本地。

| 元件 | 工具 / 說明 |
|------|------------|
| 音訊來源 | 麥克風輸入、系統喇叭輸出 |
| 虛擬混音 | VoiceMeeter（Windows）/ BlackHole（macOS） |
| 錄音腳本 | Python — `sounddevice` 或 `PyAudio` |
| 輸出格式 | 定時切割 + 壓縮為 MP3，存至本地資料夾 |

---

## 第二階段：AI 轉譯 & 結構化摘要

**目標**：將錄製完成的音訊自動上傳、轉成文字，並生成結構化摘要。

### 處理步驟

1. **偵測新檔案** — 本地資料夾有新 MP3 時，自動上傳至雲端（Google Drive / Dropbox）
2. **Webhook 觸發** — 雲端上傳完成後觸發自動化平台（Make / Zapier）
3. **語音轉文字** — 將音訊傳送至 OpenAI Whisper API，取得逐字稿
4. **結構化摘要** — 將逐字稿 + Prompt 傳送至 Claude API，生成 Markdown 格式摘要

### 使用工具

- **雲端儲存**：Google Drive / Dropbox
- **自動化平台**：Make / Zapier
- **語音轉文字**：OpenAI Whisper API
- **摘要生成**：Claude API

---

## 第三階段：自動化分發

**目標**：將摘要結果自動推送至各目標平台。

| 輸出目標 | 用途 |
|---------|------|
| Google 日曆 | 建立會議行程與摘要內文 |
| Discord / Slack / LINE | 發送即時通知 |
| 本地資料夾 / Obsidian / Notion | 備份 Markdown 筆記 |
