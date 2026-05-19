# Session Handoff — Blog-Research-Feed

## 项目目标
构建一个 AI 行业新闻汇总器（cron 定时拉取，喂给 LLM 做摘要）。仓库名 `Blog-Research-Feed` 就是干这个的。

最终架构（三层）：
1. **RSS 发现层**（免费）：feedparser 拉所有 source URL 的 feed，diff 出新 URL
2. **正文抓取层**（按需付费）：RSS 不带全文的 → Firecrawl 抓 markdown
3. **X 帖子层**：X API pay-per-use 拉关注账号的新 tweet

## 当前状态（branch: `claude/cloud-environment-testing-e2cRQ`）

### 已完成
- `sources.md` — 完整订阅清单（中英文混合，分 9 类）
- `sources.opml` — 可导入 Feedly/Inoreader 的 OPML 文件
- `.gitignore` + `.env.example` — 配置模板
- `.env` — 真实 API keys（**不在 git**）：
  - `FIRECRAWL_API_KEY=fc-4301b1c79b05468eafbaa91a006eb6e4`
  - `X_BEARER_TOKEN=AAAAAAAA...` （详见 `.env`）
- `.mcp.json` — Firecrawl MCP server 配置（用 `${FIRECRAWL_API_KEY}` 引用 env）
- `permission_test.txt` — 早期权限测试遗留，可删

### ⚠️ 已知问题 / 待办
1. **网络白名单未生效** — 当前 session 容器仍然 403 拒绝：
   - `api.firecrawl.dev`
   - `api.twitter.com` / `api.x.com`
   - 用户已在 web UI 改过 environment network policy 但没生效
   - **解决**：起新 session（环境变更通常对新建 session 才应用）+ 确认改的是当前 session 用的那个 environment

2. **`.env` 不会传给新 session** — 容器 ephemeral，`.env` 不在 git。两个选项：
   - 推荐：去 web UI 把 `FIRECRAWL_API_KEY` 和 `X_BEARER_TOKEN` 配成 **environment variables**（更安全，存在平台层，所有新 session 自动有）
   - 备选：把 key 写到 environment **setup script** 里 export
   - 不要把 `.env` commit

3. **MCP 在当前 session 不可用** — `.mcp.json` 已 commit，但 MCP server 是 session 启动时初始化的，新 session 才能用 `mcp__firecrawl__*` 工具

4. **API key 安全风险** — 两个 key 在 chat 里以明文出现过，建议**用完后 rotate**：
   - Firecrawl dashboard
   - X developer portal

### 还没动的
- 没写任何代码（aggregator 本身）
- 没建 Python venv / requirements.txt
- 没装 feedparser / firecrawl-py / tweepy
- 没设 cron / GitHub Action

## 关键设计决策（已和用户讨论过）

### 为什么 RSS + Firecrawl 混用，不全用 Firecrawl
- 纯 Firecrawl 每天要爬整个 blog index 判断新文章，30 篇 × $0.005 × 7 天 = $1/周
- RSS + Firecrawl：RSS 免费拿新 URL，只 Firecrawl 那 1 篇新文章 = $0.005/周
- 订 50 个博客差距：$50/月 vs $0.25/月

### 为什么需要 X API
- Karpathy 等大牛重要内容**长文**会回到博客（订 RSS 够），但**临时 thread**只在 X
- X API 2026.2 改成 pay-per-use，$0.005/读，去重，订 30 个账号约 $5-10/月
- 替代方案：RSSHub 自部署（免费但不稳）/ rss.app（$9/月）/ 直接在 X 关注（不进 reader）

### 内容源清单分 9 类
1. 18 篇必读爆款（每个对应作者 feed URL）
2. 官方 lab blog（Anthropic / OpenAI / DeepMind / 中国厂）
3. 个人研究者博客（4 tier）
4. Podcast
5. X 必关注账号
6. 开源工具发布渠道（agent framework / Claude Code 生态 / eval / inference / coding agent / RAG / browser-use / MCP）
7. Leaderboards
8. 新闻聚合源（英文 + 中文）
9. 订阅工具建议

详见 `sources.md` 和 `sources.opml`。

### Claude Code 生态那一节已修正过
用户指出原列表（VoltAgent / Conductor / Shipyard 等）质量不高。已查清真正主流的是：
- 官方：`anthropics/claude-plugins-official`（101 plugin）
- 社区：Obra Superpowers（40.9k ⭐）、claude-code-unified-agents、wshobson/agents
- 目录：claudemarketplaces.com、buildwithclaude.com
- **但 `sources.md` / `sources.opml` 还没改**，下个 session 要更新

## 用户偏好 / 沟通风格备注
- 用户说中文，回复也用中文
- 喜欢简洁，不爱看长长的"我即将做什么"的开场白
- 经常质疑 / 让重新查证，配合 web search 验证比直接断言更好
- 喜欢 fan-out subagent 做并行 research + 后接 reviewer 评审 —— 已经验证这套 workflow 好用
- 不信任 Claude 内置 `/goal`（觉得评审窗口太短），更喜欢自定义 Stop hook 思路
- 有 stop-hook-git-check.sh 在 `~/.claude/`，untracked 文件会被催 commit

## 新 session 启动后建议第一步
```bash
cat HANDOFF.md
git log --oneline -10
ls -la
cat .env  # 如果存在
```

然后问用户优先级：
- A) 修正 `sources.md` 里 Claude Code 那节
- B) 开始写 aggregator 代码骨架（Python + feedparser + Firecrawl + SQLite + cron）
- C) 先验证 Firecrawl / X API 在新 session 里能通了（网络白名单 + env var）
- D) 别的
