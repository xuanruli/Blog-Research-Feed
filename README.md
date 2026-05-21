# Blog Research Feed

每天 9am 自动跑的 AI engineering signal 策展 agent：在 Anthropic Managed Agents 里 host 一个 Claude，cron 每天拉昨天的 frontier engineering 内容（RSS / X / Firecrawl-index / YouTube / podcast），agent 自己 triage + 深挖 15-20 条候选 + 挑出 Top 10 写报告，最后发 Slack。

不是行业新闻简报——是给做 VLM / video agent / multimodal / coding-agent 的工程师挑昨天最值得读的 10 条 frontier signal，每条带 agent 自己的 takeaway。

## 架构（一句话版）

`brf` CLI 在容器内预装（via environment pip packages），secret 通过 Files API 每次 session 上传一个临时 `.env` 挂到 `/workspace/.env`，agent 用 `bash + brf | jq | brf` pipeline 自己干活。详细设计见 [`BRF_FETCHER_DESIGN.md`](./BRF_FETCHER_DESIGN.md) 和 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。

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
   $ brf fetch-all --since YESTERDAY                  # → /tmp/feed/index.json
   $ jq -r '.[] | "\(.id)\t\(.source)\t\(.title)"' /tmp/feed/index.json | ...
   $ brf fetch-full --id <id>                         # drill down on candidates
   $ brf report slack --message-file /tmp/report.md
```

## 仓库导览

| 路径 | 用途 |
|---|---|
| `brf/` | CLI bundle (`pip install -e .` → `brf` 命令) |
| `brf/main.py` | Click 入口：`fetch-all` / `fetch-full` 是主流程；`fetch rss/x-user/...` + `firecrawl` + `report` 是辅助 |
| `brf/feed_item.py` | `FeedItem` dataclass — 跨 fetcher 的统一 schema (id / source_type / has_full / needs_firecrawl / extra) |
| `brf/aggregator.py` | `FeedAggregator` — 并发跑所有 fetcher，按 URL 去重，写 `index.json` + 调度 `fetch_full` |
| `brf/fetchers/` | `SourceFetcher` ABC + 5 个实现：`rss.py` · `x.py` · `youtube.py` · `podcast.py` · `firecrawl_index.py` |
| `brf/sources.yaml` | 单一 source of truth：RSS feeds + X handles + YouTube channels + podcasts + firecrawl_index 配置 |
| `brf/sources_config.py` | `sources.yaml` loader + active-filter helpers |
| `brf/rss.py` · `x_client.py` · `youtube.py` · `podcast.py` | 旧 per-source 客户端，被新 fetcher 复用 + 保留为 `brf fetch <subcmd>` 旧 CLI 入口 |
| `brf/firecrawl_client.py` | Firecrawl SDK v4 wrapper（scrape + search） |
| `brf/slack.py` | Markdown → Slack mrkdwn + Block Kit |
| `agent/system_prompt.md` | Agent 的 system prompt（Top-10 ranked output 格式） |
| `agent/agent.yaml` / `environment.yaml` | Managed Agent / Environment 声明（按 `name:` 查表，不用 ID） |
| `scripts/create_agent.py` | 一次性创建/更新 agent + environment |
| `orchestrator/daily.py` (host) | Cron 端编排器：build .env、Files API upload、开 session、流式日志 |
| `.github/workflows/daily.yml` | GitHub Action cron（09:00 UTC = 17:00 北京） |
| `docs/managed_agents/` · `docs/agent_sdk/` | Anthropic Managed Agents + Agent SDK 完整文档（本地副本） |
| `BRF_FETCHER_DESIGN.md` | Phase 2-4 的 fetcher 重构设计 doc |
| `SOURCES_HEALTH.md` | 源健康审计 |

## 一次性 setup

### 1. 准备 API keys

需要：
- `ANTHROPIC_API_KEY` — Anthropic API
- `FIRECRAWL_API_KEY` — Firecrawl (https://firecrawl.dev)
- `X_BEARER_TOKEN` — X API v2 (https://developer.x.com)
- `OPENAI_API_KEY` — Whisper transcript (~$0.006/min)
- `SLACK_WEBHOOK_URL` — Slack incoming webhook，要发的频道里建一个

复制 `.env.example` → `.env` 填进去。

### 2. 创建 Managed Agent + Environment

```bash
pip install -e .
python scripts/create_agent.py
```

它会：
1. 调用 `client.beta.environments.create()` 创建 cloud 环境（pip install `blog-research-feed` from PyPI + apt jq/ffmpeg）— 这一步在 Anthropic 那边 build container image，会比较慢（30s-2min）
2. 调用 `client.beta.agents.create()` 注册 agent（system prompt + 内置 toolset，**无 custom tool**）
3. 打印 agent_id + env_id 到 stdout —— **这俩 ID 你不用动**，orchestrator 每次启动时按 `name`（来自 `agent/agent.yaml` / `environment.yaml`）到 Anthropic API 查实时 ID

需要改 agent 配置（system prompt / tools）时重跑 `python scripts/create_agent.py --update`（创建新版本）。改 `environment.yaml`（加包 / bump brf 版本）时：bump `name:` 到下一个 `v{N+1}` 然后重跑 `--update`，会自动建新 env。orchestrator 下次跑按新 name 查表，**不需要更新任何 secret**。

### 3. 配 GitHub Secrets

仓库 Settings → Secrets → Actions，加 **5 个**：
- `ANTHROPIC_API_KEY` — orchestrator 用来调 sessions/files API
- `FIRECRAWL_API_KEY` · `X_BEARER_TOKEN` · `OPENAI_API_KEY` · `SLACK_WEBHOOK_URL` — 这 4 个 orchestrator 在每次 run 时打包成 `.env` 上传给容器；agent 在容器里 `brf` 自动读 `/workspace/.env`

（agent_id / env_id **不用配** — 按 yaml 里的 `name:` 查表得来）

### 4. 试跑

手动触发：仓库 Actions → "daily-ai-news" → Run workflow。

或本地 dry-run：
```bash
python -m orchestrator.daily --dry-run
```

## 发新版 brf 到 PyPI + 重建 env

```bash
# 1. bump version
sed -i 's/version = ".*"/version = "0.2.5"/' pyproject.toml

# 2. build + upload
rm -rf dist/ build/ *.egg-info
python -m build
TWINE_USERNAME=__token__ TWINE_PASSWORD=$PYPI_API_TOKEN twine upload dist/blog_research_feed-0.2.5*

# 3. bump env name + pin in agent/environment.yaml
#    name: blog-research-feed-env-v8 → v9
#    "blog-research-feed==0.2.4"      → "==0.2.5"

# 4. provision the new env + push the new system prompt
python scripts/create_agent.py --update
```

下次 cron 自动按新 name 拉新 env。

## CLI 烟测

**主流程**（统一 fetch-all / fetch-full，agent 默认就用这两个）：

```bash
brf fetch-all --since 2026-05-20 --output-dir /tmp/feed       # 并发拉所有源 → /tmp/feed/index.json
jq 'length' /tmp/feed/index.json                              # 总条数
jq -r 'group_by(.source_type)|map({type:.[0].source_type,n:length})|.[]' /tmp/feed/index.json
brf fetch-full --id <id> --output-dir /tmp/feed               # drill 单条 → /tmp/feed/full/<id>.{html,txt,md}
cat /tmp/feed/full/<id>.html
echo "# report" | brf report slack --message-file /dev/stdin
```

**辅助命令**（少用，仅当 `fetch-all` 没覆盖时）：

```bash
brf fetch x-user --handle <handle> --since 2026-05-20         # 拉某个未在 sources.yaml 配置的 X 账号
brf fetch youtube-transcript --url https://www.youtube.com/watch?v=xxx
brf fetch podcast-transcript --url https://feeds.transistor.fm/the-cognitive-revolution
brf firecrawl scrape --url https://cognition.ai/blog          # 任意单页全文
brf firecrawl search --query "frontier model release 2026" --limit 10
```

`fetch-all` 输出的每个 `FeedItem` 形状（详见 `brf/feed_item.py`）：

```json
{
  "id": "ab12cd34ef567890",
  "source_type": "rss",
  "source": "Simon Willison's Weblog",
  "title": "...",
  "url": "https://...",
  "published": "2026-05-19T14:23:00+00:00",
  "summary": "≤500 字符",
  "has_full": true,
  "needs_firecrawl": false,
  "extra": {}
}
```

## 已知限制

- Whisper 单文件 25MB cap，长 podcast 会被拒（status `too_large`）。System prompt 限制每天 ≤ 2 个转录控制成本（~$0.36/30min）
- 部分中文源 RSS 死链，靠 `firecrawl_index` 兜底（见 `SOURCES_HEALTH.md`）
- SSE stream 断线不会自动重连（45min timeout 内单次任务通常不会触发）
- YouTube channel RSS 偶尔被 YouTube 给本机出口 IP 限流（404/500），prod 环境 IP 正常；`YouTubeFetcher` 内部 swallow 异常，不会 sink 整次 run

## 开发

```bash
pip install --upgrade "setuptools<72" wheel
pip install -e .[dev]   # 装 pytest 等

pytest -q                                # 92 个 unit test
brf fetch-all --since 2026-05-20         # 端到端冒烟（成本 ~$0.2 firecrawl）

python scripts/create_agent.py --update  # 推 agent.yaml / system_prompt.md / environment.yaml 改动到 Anthropic
```

## 历史

- **2026-05-19 v0**：scaffold 完整管线 — 7 个 custom tool + orchestrator dispatch
- **2026-05-19 v1**：架构重构 — 删除 custom tool，改用 Files API mount `.env` + `bash + brf` pipe
- **2026-05-20 Phase 1**：`FeedItem` schema + `SourceFetcher` ABC + `FeedAggregator` skeleton + `sources.yaml`（PR #12）
- **2026-05-20 Phase 2**：`RssFetcher` (3-branch normalize: full / summary / title-only) + 把旧 `brf fetch rss` delegate 到新 fetcher（PR #13）
- **2026-05-20 Phase 3**：`XFetcher` + `YouTubeFetcher` + `PodcastFetcher`，外加跨 fetcher dedupe 优先级修复（PR #14）
- **2026-05-20 Phase 4**：`FirecrawlIndexFetcher` for no-feed sites（PR #15）
- **2026-05-21 Phase 5**：System prompt 改写为 fetch-all/fetch-full 流；firecrawl-py v4 SDK fix；HF Daily Papers regex / yymm 日期月末戳；URL fragment 去重（PR #16）
- **2026-05-21 Source refocus**：砍掉新闻聚合源（Marginal Revolution / Stratechery / TLDR AI / Import AI 等 13 个），加 VLM / multimodal / coding-agent 风向标账号（HF blog / Roboflow / Claude Code & Codex release feeds / 8 个新 X handles / 9 个新 YouTube channels）；输出格式改为严格 Top 10 ranked list（PR #17）
- **2026-05-21 0.2.4 上线 + env-v8**：brf 0.2.4 发 PyPI，env bump v7→v8 修复 CI（PR #18）
