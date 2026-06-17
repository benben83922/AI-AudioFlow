# LLM 服務（OpenAI 相容 → claude -p）

一支**獨立**的程式：對外提供 OpenAI 相容的 `POST /v1/chat/completions`，對內以 `claude -p`（Claude 訂閱）作為後端。供 OpenClaw 透過 HTTP 呼叫，做逐字稿整理／摘要等 LLM 工作。

```
OpenClaw ──HTTP(OpenAI 格式)──▶ 本服務 ──內部 claude -p──▶ Claude（訂閱）
```

## 為什麼這樣設計

- **與 OpenClaw 解耦**：OpenClaw 只認 HTTP 合約，不知道後端是 CLI 還是 API。
- **未來可零成本換後端**：要從 `claude -p` 換成直接打 API → 改本服務內部即可；OpenClaw 一字不用動。甚至可讓 OpenClaw 直接把 base_url 指向別的 OpenAI 相容端點。
- **本服務不碰檔案**：純「messages 進 / completion 出」。讀寫逐字稿是 OpenClaw 的責任。
- **跟 whisper 同一個慣例**（OpenAI 相容 HTTP），整個系統風格一致。

## 認證（訂閱）

在你**本機**（已登入 Claude）跑一次，產生長效 token：

```bash
claude setup-token
```

把 token 放進 env 檔（別直接貼進指令，會留在 shell 歷史）：

```bash
echo "CLAUDE_CODE_OAUTH_TOKEN=貼上你的token" > llm.env
```

## 啟動

建議 OpenClaw 與本服務接同一個 docker network，用**容器名**互連，不對主機開 port：

```bash
docker network create audioflow-net          # 一次性

docker build -t llm-service .
docker run -d --name llm-service \
  --network audioflow-net \
  --env-file llm.env \
  llm-service
# 想從主機直接測，再加 -p 8088:8088
```

## OpenClaw 怎麼呼叫（任何 OpenAI 客戶端）

```python
from openai import OpenAI

client = OpenAI(base_url="http://llm-service:8088/v1", api_key="not-needed")

resp = client.chat.completions.create(
    model="opus",
    messages=[
        {"role": "system", "content": "你是中文逐字稿整理助手。只輸出整理後的 Markdown。"},
        {"role": "user", "content": transcript_text},
    ],
)
markdown = resp.choices[0].message.content
```

> `api_key` 隨便填 —— 本服務不檢查它，真正的認證是容器內的 `CLAUDE_CODE_OAUTH_TOKEN`。

## 未來：OpenClaw 改成 call API

因為介面是 OpenAI 相容的，切換時 **OpenClaw 的呼叫程式碼不用改**，只動設定：
- 把 `base_url` 從 `http://llm-service:8088/v1` 指向真正的 API 端點，`api_key` 換成真金鑰；或
- 讓本服務內部把 `claude -p` 換成 API 呼叫（OpenClaw 連 base_url 都不用改）。

## 設定

| 環境變數 | 預設 | 說明 |
|----------|------|------|
| `PORT` | `8088` | 服務埠 |
| `CLAUDE_MODEL` | `opus` | 傳給 `claude --model`（別名或完整 id） |
| `CLAUDE_CODE_OAUTH_TOKEN` | （必填） | 訂閱 token，由 `claude setup-token` 取得 |

## 健康檢查

```bash
curl http://localhost:8088/health        # → ok
```

## 注意

- `--dangerously-skip-permissions`：純文字任務、且本服務**零檔案掛載**，加它只是確保 headless 不會卡在權限詢問，影響範圍極小。
- 多輪對話會以 `User:` / `Assistant:` 標籤串接後餵給 `claude -p`；單輪（最常見）就是原文。
