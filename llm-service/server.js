// OpenAI 相容的 LLM 服務 — 內部以 `claude -p`（訂閱）作為後端。
//
// 對外合約：POST /v1/chat/completions（OpenAI Chat Completions 格式）
// 對內實作：把 messages 餵給 `claude -p`，回傳的文字包成 OpenAI 回應。
//
// 與 OpenClaw 解耦：OpenClaw 只認這個 HTTP 合約，不知道後端是 CLI 還是 API。
// 未來要換成直接打 Anthropic API，只改這支服務內部（甚至 OpenClaw 直接改 base_url），
// OpenClaw 的呼叫程式碼一字不用動。
//
// 認證：訂閱 token 走環境變數 CLAUDE_CODE_OAUTH_TOKEN（claude CLI 自動讀取）。

const http = require("http");
const { spawn } = require("child_process");

const PORT = process.env.PORT || 8088;
const MODEL = process.env.CLAUDE_MODEL || "opus";
// claude -p 子程序逾時（毫秒）。逾時即殺掉，避免 claude 卡住時請求永久懸著。
// 預設 600s，與 openclaw 端的 LLM_TIMEOUT 一致；可用 CLAUDE_TIMEOUT_MS 覆寫。
const CLAUDE_TIMEOUT_MS = parseInt(process.env.CLAUDE_TIMEOUT_MS || "600000", 10);

// 把 OpenAI 的 content（字串或 parts 陣列）攤平成純文字
function contentToText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => p.text || "").join("");
  return "";
}

// 以 claude -p 跑一次：對話走 stdin，system 走 --append-system-prompt
function runClaude(conversation, systemText) {
  return new Promise((resolve, reject) => {
    const args = [
      "-p",
      "--model", MODEL,
      "--output-format", "text",
      "--dangerously-skip-permissions", // 純文字、無掛載，確保 headless 不卡權限
    ];
    if (systemText) args.push("--append-system-prompt", systemText);

    const child = spawn("claude", args, { stdio: ["pipe", "pipe", "pipe"] });
    let out = "", err = "", settled = false;

    // 逾時守衛：先 SIGTERM，5s 後仍在就 SIGKILL，並以錯誤結束 Promise
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { child.kill("SIGTERM"); } catch (_) {}
      setTimeout(() => { try { child.kill("SIGKILL"); } catch (_) {} }, 5000);
      reject(new Error(`claude 逾時（${CLAUDE_TIMEOUT_MS}ms）已中止`));
    }, CLAUDE_TIMEOUT_MS);

    const finish = (fn, arg) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(arg);
    };

    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    child.on("error", (e) => finish(reject, e));
    child.on("close", (code) =>
      code === 0
        ? finish(resolve, out)
        : finish(reject, new Error(err.trim() || `claude exit ${code}`))
    );
    child.stdin.write(conversation);
    child.stdin.end();
  });
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    return res.end("ok");
  }
  if (req.method !== "POST" || !req.url.startsWith("/v1/chat/completions")) {
    res.writeHead(404, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ error: { message: "not found" } }));
  }

  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    try {
      const payload = JSON.parse(body);
      const messages = payload.messages || [];
      const systemText = messages
        .filter((m) => m.role === "system")
        .map((m) => contentToText(m.content))
        .join("\n\n");
      // 多輪時加上角色標籤；單輪（最常見：一段 user）就是原文
      const nonSystem = messages.filter((m) => m.role !== "system");
      const conversation =
        nonSystem.length <= 1
          ? contentToText(nonSystem[0]?.content)
          : nonSystem
              .map((m) => `${m.role === "assistant" ? "Assistant" : "User"}: ${contentToText(m.content)}`)
              .join("\n\n");

      const text = await runClaude(conversation, systemText);

      const resp = {
        id: "chatcmpl-claude-" + Date.now(),
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model: MODEL,
        choices: [
          { index: 0, message: { role: "assistant", content: text.trim() }, finish_reason: "stop" },
        ],
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      };
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(resp));
    } catch (e) {
      console.error("[llm-service] error:", e.message);
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: { message: String(e.message || e) } }));
    }
  });
});

server.listen(PORT, () =>
  console.log(`[llm-service] OpenAI 相容介面 on :${PORT}，後端 = claude -p (${MODEL})`)
);
