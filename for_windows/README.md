# AI AudioFlow — 純 Windows 版

把原系統重新整理成**只在 Windows 本機執行**的版本：捨棄 Docker、WSL、docker-compose、
`llm-service`、`openclaw` 與 whisper 容器。每次錄音完成後，背景 worker 自動在 Windows
原生跑「轉譯（faster-whisper）→ 整理（`claude -p`）」，產出逐字稿與會議紀錄。

> 這是獨立的一套，與專案根目錄的原 Docker/WSL 版本並存、互不影響。

---

## 架構

```
錄音 app（pywebview GUI，Windows 原生）
  │ 錄完原子化落地 .wav
  ▼
recordings/ ──▶ 處理 worker（detached，輪詢；關 app 後仍續跑）
                  │ 階段 1：faster-whisper 在行程內轉譯
                  ▼
                transcripts/*.txt
                  │ 階段 2：claude -p 整理（雙偵測：Windows 原生優先，WSL 後備）
                  ▼
                results/*.md（會議紀錄）
```

全程**無 Docker、無 WSL 依賴**（claude 在 WSL 時才用到 WSL，作為後備）、無跨界檔案掛載、
無 HTTP 服務。資料夾全在 Windows 本機（預設於工作目錄；可於設定覆寫）。

### 與原系統的對應

| 原系統 | 純 Windows 版 |
|--------|--------------|
| whisper 容器（HTTP `/v1/audio/transcriptions`） | `src/stt_engine.py`：faster-whisper **in-process** |
| llm-service（Node，OpenAI 相容外殼） | 移除 |
| openclaw（容器，輪詢整理） | 併入處理 worker 的階段 2 |
| docker-compose / .env / WSL 路徑偵測 | 移除 |
| 「沒 Docker 不給錄」把關 | 改為「沒音訊裝置不給錄」；claude 只擋自動整理、不擋錄音 |

---

## Claude 雙偵測

「整理」階段呼叫 `claude -p`（訂閱模式）。`src/claude_cli.py` 依序偵測：

1. **Windows 原生**：`PATH` 或常見安裝位置 + `claude --version` 確認可執行。
2. **WSL 後備**：`wsl -e bash -lc "command -v claude"`（使用者把 claude 裝在 WSL 時）。

兩者皆無 → 前端橫幅 / 服務頁提示安裝，但**錄音與轉譯照常**，逐字稿保留、待 claude 就緒後
自動補整理（worker 下一輪掃描），或於清單手動「生成會議紀錄」。

完整 prompt（system + 指令 + 逐字稿）走 **stdin** 餵入，避開 Windows↔WSL 的引號與 CJK
編碼問題。認證：

- Windows 模式：`CLAUDE_CODE_OAUTH_TOKEN`（設定填入）帶入子程序環境。
- WSL 模式：透過 `WSLENV` 共享 token 進 WSL；或使用者已在 WSL `claude setup-token` 登入則留空即可。

> Claude CLI 由**使用者自行安裝**（本程式不代裝、不打包它）；安裝指引：
> <https://docs.claude.com/en/docs/claude-code/setup>

---

## 執行（原始碼）

需求：Python ≥ 3.11、Windows、（自動整理需）已安裝並登入 Claude CLI。

```bat
cd for_windows
run.bat
```

首次啟動會自動 `pip install -e .`（需連網），裝好 `pywebview / sounddevice / soundfile /
faster-whisper` 等。GUI 起來後走「首次設定」填錄音資料夾與（可選）Claude token，按「完成設定」
即啟動背景處理 worker。

- 手動跑 worker（除錯用）：`python -m src.main --worker`
- worker log：工作目錄下 `worker.log`

### 資料夾

| 路徑 | 用途 |
|------|------|
| `recordings/` | 錄音原始 .wav（可於設定改到別處，如 `D:\record`） |
| `transcripts/` | 逐字稿 .txt |
| `results/` | 會議紀錄 .md |
| `models/` | faster-whisper 模型快取（只下載一次；可預先放好達成零下載） |

打包後（frozen）以上位於 `%LOCALAPPDATA%\AudioFlow`。

---

## STT 效能 / GPU

預設 `large-v3-turbo` + CPU + `int8`（無 GPU 依賴，但長錄音非即時）。設定頁可改：

- 有 NVIDIA GPU：`device=cuda`、`compute_type=float16` 大幅加速（需 CUDA/cuDNN runtime）。
- 模型 / 語言亦可調。改完需**重啟處理服務**才生效。

---

## 打包成單一 exe（概要）

- **Python 套件 + native 擴充（faster-whisper / ctranslate2 等）**：Nuitka onefile 可全部內嵌，
  打包後不跑 pip、零啟動下載。
- **whisper 模型（~1.5GB）**：建議隨包附 `models/`（sidecar）或嵌入 onefile 並設
  `--onefile-tempdir-spec` 固定快取（只解壓一次）。預先放好即可「執行時不下載」。
- **GPU runtime（cuDNN/cuBLAS）**：要 GPU 加速才需一起帶，體積較大；CPU-only 可省。
- **Claude CLI**：**不**打包，由使用者於 Windows 或 WSL 自行安裝；啟動時雙偵測其存在。

worker 以內嵌模式重新拉起主 exe（`app.exe --worker`），與原系統一致。
