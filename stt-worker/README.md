# STT worker

獨立元件，在 **WSL2 原生**執行。輪詢錄音資料夾，把音檔送 whisper 容器轉譯，逐字稿原子化寫到輸出資料夾供 openclaw 後續消費。

```
錄音 app → D:\record ──(worker 輪詢)──▶ whisper 容器 (HTTP) ──▶ ~/stt-outbox ──▶ openclaw
```

## 為什麼這樣設計

- **與 openclaw 解耦**：worker 只做「音檔 → 逐字稿」；openclaw 只吃逐字稿。任一邊壞掉/抽換不影響另一邊。
- **走 HTTP 傳 bytes**，不靠檔案掛載 → 不需要把音檔複製到 ext4，也沒有跨界 inotify 問題。
- **輪詢而非檔案事件**：`/mnt/d` 的 inotify 跨界不可靠，故用輪詢。
- **原子化**：只處理已落地的完成檔（錄音端 temp→rename）；逐字稿輸出同樣 temp→`os.replace`。
- **冪等**：outbox 已有同名逐字稿就跳過，重啟不重跑。

## 依賴

只需要 `httpx`（已在主專案 `pyproject.toml`）。

```bash
pip install httpx        # 或在主專案的 venv 內已具備
```

## 啟動 whisper 容器（先決條件）

```bash
docker run -d \
  --name whisper-stt-server \
  --restart=always \
  -p 9000:9000 \
  -v whisper-data:/var/lib/whisper \
  -e WHISPER_MODEL=large-v3-turbo \
  -e WHISPER_DEVICE=cpu \
  -e WHISPER_COMPUTE_TYPE=int8 \
  -e WHISPER_THREADS=8 \
  hwdsl2/whisper-server:<pin版本>
```

## 執行 worker

```bash
cd stt-worker
cp .env.example .env        # 視需要編輯
set -a; . ./.env; set +a    # 載入環境變數
python3 worker.py
```

完成標準：錄一段音，稍候後在 `~/stt-outbox` 看到同名逐字稿（預設 `.txt`）。

## 設定

全部走環境變數，見 [`.env.example`](./.env.example)。常用：

| 變數 | 預設 | 說明 |
|------|------|------|
| `STT_WATCH_DIR` | `/mnt/d/record` | 監看的錄音資料夾 |
| `STT_OUTBOX_DIR` | `~/stt-outbox` | 逐字稿輸出 |
| `WHISPER_URL` | `http://localhost:9000` | whisper 容器位址 |
| `STT_LANGUAGE` | （空＝自動） | 例：`zh` |
| `STT_RESPONSE_FORMAT` | `text` | `text`/`json`/`srt`/`vtt` |
| `STT_POLL_INTERVAL` | `5` | 輪詢秒數 |
| `STT_REQUEST_TIMEOUT` | `600` | 單檔逾時（長錄音調大） |

## 待辦 / 可強化

- 失敗的檔目前只記 log、下輪重試；可加「毒丸」標記避免無限重試卡住。
- 長錄音可加切段送出再合併。
- 可選 systemd unit 讓它開機常駐。
