# Blog Research Feed

每天 9am 自动跑的 AI 新闻策展 agent：在 Anthropic Managed Agents 里 host 一个
Claude，cron 每天拉昨天的内容（RSS / X / Firecrawl / YouTube / podcast），
agent 自己决定哪些深挖、写报告，最后发 Slack。

## 架构（一句话版）

`brf` CLI 在容器内预装（via environment pip packages），secret 通过 Files
API 每次 session 上传一个临时 `.env` 挂到 `/workspace/.env`，agent 用
`bash + brf | jq | brf` pipeline 自己干活。详细 ASCII 图见
[`ARCHITECTURE.md`](./ARCHITECTURE.md)。

```
 GitHub Action (cron 09:00 UTC)
   ↓ python -m orchestrator.daily
 Orchestrator:
   1. build .env payload from host env vars
   2. files.upload(.env)
   3. sessions.create(resources=[file mount at /workspace/.env])
   4. stream events for logs, exit on idle/terminated
   5. files.delete(uploaded .env)
 Inside container (Anthropic cloud):
   $ brf fetch rss --since YESTERDAY > /tmp/rss.json
   $ brf firecrawl scrape --url <interesting> > /tmp/scrape.json
   $ brf report slack --message-file /tmp/report.md
```

**v0 → v1 重构**：从 7 个 custom tool + orchestrator dispatch 改成 CLI +
bash pipe + Files API secret mount。优势：agent 能 `brf | jq | brf` 组合，
不用每个 tool call 一次往返 host。代价：secret 在容器里（依赖容器隔离 +
prompt injection 防御，权衡见 ARCHITECTURE §3）。

## 仓库导览

| 路径 | 用途 |
|---|---|
| `brf/` | CLI bundle (`pip install -e .` → `brf` 命令) |
| `brf/main.py` | Click 入口，子命令 `fetch` / `firecrawl` / `report` / `daily` |
| `brf/` (容器内) | 纯 tool CLI：fetch/firecrawl/report。pip 安装到 agent 容器里 |
| `orchestrator/daily.py` (host) | Cron 端编排器：build .env、Files API upload、开 session、流式日志 |
| `brf/rss.py` | feedparser + sources.opml，跳过 SOURCES_HEALTH 标记的死链 |
| `brf/x_client.py` | X API v2，优雅处理 402 no_credits |
| `brf/firecrawl_client.py` | Firecrawl SDK v2 scrape/search |
| `brf/youtube.py` | youtube-transcript-api + oEmbed 抓 transcript |
| `brf/podcast.py` | RSS → mp3 → OpenAI Whisper (25MB cap) |
| `brf/slack.py` | Markdown → Slack mrkdwn + Block Kit |
| `agent/` | Managed Agent 配置（`system_prompt.md` + `agent.yaml` + `environment.yaml`） |
| `scripts/create_agent.py` | 一次性创建/更新 agent，打印 ID 用于 GitHub Secrets |
| `.github/workflows/daily.yml` | GitHub Action cron |
| `docs/managed_agents/` | Anthropic Managed Agents 完整文档（17 篇，本地）|
| `docs/agent_sdk/` | Claude Agent SDK 文档（16 篇，本地）|
| `sources.opml` / `sources.md` | 订阅源清单（60 RSS + 30 无 feed 站 + 45 X 账号）|
| `SOURCES_HEALTH.md` | 源健康审计（哪些死链、哪些需 Firecrawl 补全文）|

## 一次性 setup

### 1. 准备 API keys

需要：
- `ANTHROPIC_API_KEY` — Anthropic API
- `FIRECRAWL_API_KEY` — Firecrawl (https://firecrawl.dev)
- `X_BEARER_TOKEN` — X API v2 (https://developer.x.com) ⚠️ 当前 0 credits
- `OPENAI_API_KEY` — Whisper transcript (~$0.006/min)
- `SLACK_WEBHOOK_URL` — Slack incoming webhook，要发的频道里建一个

复制 `.env.example` → `.env` 填进去。

### 2. 创建 Managed Agent + Environment

```bash
pip install -e .
python scripts/create_agent.py
```

它会：
1. 调用 `client.beta.environments.create()` 创建 cloud 环境（带 pip
   install `brf` from git + apt jq/ffmpeg）— 这一步在 Anthropic 那边
   build container image，会比较慢（30s-2min）
2. 调用 `client.beta.agents.create()` 注册 agent（system prompt + 内置
   toolset，**无 custom tool**）
3. 打印两行到 stdout：
   ```
   ANTHROPIC_AGENT_ID=agent_...
   ANTHROPIC_ENV_ID=env_...
   ```

把这两个值加到 `.env` 和 GitHub Repository Secrets。

需要改 agent 配置时重跑 `python scripts/create_agent.py --update`（创建新版本）。

### 3. 配 GitHub Secrets

仓库 Settings → Secrets → Actions，加 7 个：
- `ANTHROPIC_API_KEY` · `ANTHROPIC_AGENT_ID` · `ANTHROPIC_ENV_ID` —
  这三个 orchestrator 自己用（创建 session、调 Files API）
- `FIRECRAWL_API_KEY` · `X_BEARER_TOKEN` · `OPENAI_API_KEY` ·
  `SLACK_WEBHOOK_URL` — 这四个 orchestrator 在每次 run 时打包成 `.env`
  上传给容器；agent 在容器里 `brf` 自动读 `/workspace/.env`

### 4. 试跑

手动触发：仓库 Actions → "daily ai news" → Run workflow。

或本地 dry-run：
```bash
python -m orchestrator.daily --dry-run
```

## CLI 烟测

每个 fetch 子命令都能独立跑（便于 debug）：

```bash
brf fetch rss --since 2026-05-18                  # 拉昨天所有 RSS items 到 stdout JSON
brf fetch x-user --handle karpathy --since 2026-05-18
brf firecrawl scrape --url https://cognition.ai/blog
brf firecrawl search --query "frontier model release 2026" --limit 10
brf fetch youtube-transcript --url https://www.youtube.com/watch?v=xxx
brf fetch podcast-transcript --url https://feeds.transistor.fm/the-cognitive-revolution
echo "# Test report" | brf report slack --message-file /dev/stdin
```

每个子命令都打印 `{...}` JSON 到 stdout，错误到 stderr，方便管道。

## 已知限制

详见 `ARCHITECTURE.md` §7：
- X API 当前 0 credits — `fetch_x_user` 会返回 `{status: "no_credits"}`，agent
  按 system prompt 跳过该工具
- Whisper 单文件 25MB cap，长 podcast 会被拒（status `too_large`）
- 12+ feed URL 已死（见 `SOURCES_HEALTH.md`）已硬编码在 `brf/rss.py SKIP_FEEDS`
- SSE stream 断线不会自动重连（30min 内单次任务通常不会触发）

## 开发

```bash
# 装依赖（feedparser 拽 sgmllib3k 老库，需要老 setuptools 才能 build）
pip install --upgrade "setuptools<72" wheel
pip install -e .

# 跑测试
python -c "import ast; [ast.parse(open(f).read()) for f in ['brf/main.py']]"
```

## 历史

- 2026-05-19 v0：scaffold 完整管线，所有模块已实现 + reviewed。详见
  `HANDOFF.md`（v0 之前的设计决策）和 `SOURCES_HEALTH.md`（源审计）。
- 2026-05-19 v1：架构重构 — 删除 7 个 custom tool 和 orchestrator dispatch
  逻辑（~180 行）；改用 Files API mount `.env` 到容器，agent 在 bash 里
  直接 pipe `brf` CLI。详见 commit message 和 `ARCHITECTURE.md`。
