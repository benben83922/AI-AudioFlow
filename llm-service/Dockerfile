# 基底用 node —— claude（Claude Code）是 Node 程式，需要 Node 才能跑
FROM node:22-slim

# 裝 Claude Code（提供 `claude` 指令）
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY server.js .

EXPOSE 8088
CMD ["node", "server.js"]
