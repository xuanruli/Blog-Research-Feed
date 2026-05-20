# Sources Health Check — 2026-05-19

针对 `sources.opml` (60 RSS) + `sources.md` (⚠️ 无 feed 站 + 𝕏 only 账号) 的健康度审计。三个 subagent 并行 fan-out (`curl -L`, UA = `Mozilla/5.0 BlogResearchFeed/1.0`, 15s timeout) 完成。

图例：
- **FULL** = `content:encoded` 或长 `description`，RSS 直接用即可
- **SUMMARY** = 仅 title / 短摘要 → **NEEDS_FIRECRAWL** 抓正文
- **BROKEN** = 404 / 死链 / 返回 HTML → 需替换 URL
- **FIRECRAWL_FALLBACK** = RSS 坏但站点活，`brf/rss.py` 走 Firecrawl 抓 index 页提条目
- **NEEDS_X_API** = X-only 账号，无 feed

---

## 1. RSS 死链 / 必须替换（共 15 条，SKIP_FEEDS）

| Feed | 状态 | 处理 |
|---|---|---|
| `raw.githubusercontent.com/conoro/anthropic-engineering-rss-feed/main/feed.xml` | 404 | 仓库/文件已删，找替代 scraper 或自建 |
| `jxnl.co/feeds/feed.xml` | 404 | 站点结构变了，找新 feed URL |
| `gwern.net/index.xml` | 301 → HTML | 用 `gwern.net/index` 不是 feed；查正确 endpoint |
| `davidstarsilver.wordpress.com/feed/` | 200 但 0 items | 空 feed；可能博主清空 |
| `www.braintrust.dev/blog/rss.xml` | 404 | 死链，Firecrawl 抓 `/blog` |
| `blog.vllm.ai/feed.xml` | 404 | Next.js 站无 feed，Firecrawl |
| `deeplearning.ai/the-batch/feed/` | 404 | feed path 变了，找新 endpoint |
| `reddit.com/r/LocalLLaMA/.rss` | 403 | Reddit 封 UA；用 OAuth 或 old.reddit.com |
| `aiera.com.cn/feed` / `zhidx.com/feed` | 500 | 服务端 error |
| `geekpark.net/rss` / `feed.infoq.cn` | 连接失败 | 容器网络拒绝（可能墙）或站点拒绝 |
| `api.substack.com/feed/podcast/68003.rss` (Dwarkesh) | 404 | show ID 变了，去 dwarkesh.com 找新 RSS |
| `feeds.transistor.fm/the-cognitive-revolution` | 404 | slug 变了 |
| `feeds.megaphone.fm/CHTH3437994392` (ChinaTalk podcast) | 404 | show ID 失效 |

## 1.5 RSS 坏但站点活 → FIRECRAWL_FALLBACK_FEEDS（共 3 条）

`brf/rss.py` 对这三条 Firecrawl 抓 `html_url` 然后正则提条目。$0.005/scrape × 3 × 30 = ~$0.45/月。

| Feed | RSS 故障 | Fallback html_url | Article URL pattern | Date |
|---|---|---|---|---|
| `www.jiqizhixin.com/rss` | XML 解析失败 | `www.jiqizhixin.com` | `/articles/YYYY-MM-DD-N` | URL 含日期 |
| `jamesg.blog/hf-papers.xml` | 502（第三方 scraper 已死，HF 官方无 feed） | `huggingface.co/papers` | `/papers/YYMM.NNNNN`（arXiv ID） | 无（arXiv ID 只到月，弃用） |
| `blog.langchain.com/rss/` | 200 但返回 HTML | `blog.langchain.com` | kebab-case slug + slug_blocklist | 无（URL 不带日期） |

---

## 2. RSS 活但只给摘要（NEEDS_FIRECRAWL 抓全文）

| Feed | 摘要长度 | 备注 |
|---|---|---|
| `eugeneyan.com/rss/` | ~86 chars | 必抓 |
| `lilianweng.github.io/index.xml` | ~1.1K | 也已停更（最新 2025-05） |
| `simonwillison.net/atom/everything/` | ~2.7K | PARTIAL，长文可能截断 |
| `embracethered.com/blog/index.xml` | ~935 chars | 边缘 |
| `zed.dev/blog.rss` | 仅摘要 | 抓全文用 |
| `tldr.tech/api/rss/ai` | 空 desc | 只有标题，得抓 |
| `news.ycombinator.com/rss` | 仅链接 | 设计如此；按需抓外链 |
| `stratechery.com/feed/` | ~3K teaser | 付费墙，全文需订阅 |
| `qbitai.com/feed` | 仅标题 | 量子位摘要无正文 |

---

## 3. RSS 可用但内容陈旧（保留，但别期望更新）

| Feed | 最新 | 距今 |
|---|---|---|
| `huyenchip.com/feed.xml` | 2025-01-16 | 16 个月 |
| `lilianweng.github.io/index.xml` | 2025-05-01 | 12 个月 |
| `neelnanda.io/blog?format=rss` | 2025-08-19 | 9 个月 |
| `github.com/humanlayer/12-factor-agents/commits.atom` | 2025-09-21 | 8 个月 |
| `github.com/inngest/agent-kit/releases.atom` | 2025-11-13 | 6 个月 |
| `github.com/SWE-agent/SWE-agent/releases.atom` | 2025-05-22 | 12 个月 |
| `buttondown.com/ainews/rss` | 2025-04-25 | 13 个月（**核实 smol.ai newsletter 是否仍在运营**） |
| `github.com/Aider-AI/aider/releases.atom` | 2026-02-12 | 3 个月 |
| `github.com/mastra-ai/mastra/releases.atom` | release notes 是空版本号占位，低信号 |

---

## 4. RSS 健康（FULL，直接用，共 ~35 条）

涵盖：karpathy / hamel / interconnects / latent.space / astralcodexten / ai-futures / registerspill / dwarkesh / oneusefulthing / chinatalk / sebastianraschka / cameronrwolfe / 几乎所有 `releases.atom`（langgraph / llama_index / dspy / crewAI / smolagents / pydantic-ai / wshobson/agents / claude-squad / inspect_ai / phoenix / weave / vllm / sglang / llama.cpp / ollama / cline / continue / OpenHands / lancedb / browser-use / stagehand / playwright / mcp spec / Arize phoenix / wandb weave）+ devblogs.microsoft.com/agent-framework + blog.vespa.ai + jack-clark + thesequence + pragmaticengineer + marginalrevolution + 36kr。

**大 payload 注意**：sglang releases ~2.7MB，vllm/llama_index ~500KB；feedparser 拉这些时设 `etag`/`modified` 避免重复下载。

---

## 5. 无 feed 站点（NEEDS_FIRECRAWL_SCRAPE）

可达（HEAD 200，Firecrawl 可直接 scrape index 页）：
- 个人/研究：cognition.ai/blog · research.trychroma.com · sh-reya.com/blog · thariq.io · darioamodei.com · incompleteideas.net
- 工具厂博客：conductor.build/changelog · shipyard.build/blog · turbopuffer.com/blog · blog.skyvern.com
- Anthropic 子站：anthropic.com/news · /research · alignment.anthropic.com · transformer-circuits.pub
- 美国 lab：deepmind.google/discover/blog · ai.meta.com/blog · mistral.ai/news · cohere.com/blog · x.ai/news · machinelearning.apple.com
- 中国 lab：moonshot.ai · zhipuai.cn
- Leaderboards：lmarena.ai · swebench.com · arcprize.org · artificialanalysis.ai
- 新闻：alphasignal.ai · lastweekin.ai · mittrchina.com

需排查：
- `openai.com/news` HEAD 403 — Firecrawl 用 browser headers 可绕
- `qwen.ai/blog` HEAD 405（拒 HEAD），GET 应该 OK
- `api-docs.deepseek.com/news/` 404 — 找正确 URL
- `research.baidu.com` / `guixingren.com` 容器内不可达（可能墙），让 Firecrawl 试

---

## 6. 𝕏 only 账号（NEEDS_X_API）— 共 45 个

按 sources.md §5 分类：

| 类别 | 数量 | 账号 |
|---|---|---|
| Lab leaders | 9 | @sama @darioamodei @demishassabis @jackclarkSF @AnthropicAI @OpenAI @GoogleDeepMind @cognition_labs @ssi_inc |
| 研究者 | 11 | @karpathy @ylecun @lilianweng @DrJimFan @rasbt @jeremyphoward @natolambert @giffmana @_jasonwei @arankomatsuzaki @cHHillee |
| Anthropic | 3 | @AmandaAskell @sleepinyourhat @repligate |
| Agent/Infra | 7 | @hwchase17 @jerryjliu0 @lateinteraction @swyx @_philschmid @samuel_colvin @tonyhb |
| 实操/Eval | 8 | @simonw @hamelhusain @eugeneyan @omarsar0 @jobergum @bclavie @OfirPress @sirupsen |
| 中国/Asia | 3 | @teortaxesTex @nrehiew_ @reach_vb |
| 安全/爆款 | 4 | @elder_plinius @emollick @AndrewYNg @mattshumer_ |

**注**：当前 X bearer token 鉴权通过但账号 0 credits（HTTP 402）。三个选项：
1. 给 X dev account 充值（pay-per-use ~$0.005/读）
2. 自建 RSSHub / Nitter 实例桥接（免费但运维成本）
3. 部分账号有 blog（karpathy / simonw / lilianweng / lambert 等）已经在 RSS 覆盖里，X 只补 thread 类内容

---

## 汇总数字

| 状态 | 数量 |
|---|---|
| RSS FULL，直接可用 | ~35 |
| RSS 摘要 only（需 Firecrawl 补正文） | 9 |
| RSS 死链/失效（SKIP_FEEDS，等替换） | 15 |
| RSS 坏但走 Firecrawl fallback | 3 |
| RSS 陈旧但活着 | 9 |
| 无 feed 站（Firecrawl 抓 index） | ~30 |
| X-only 账号（需 X API / 桥接） | 45 |

---

## 下一步建议优先级

1. **修死链**：先处理 5 个 podcast/feed 重定向（Dwarkesh / Cognitive Revolution / ChinaTalk podcast / The Batch / langchain blog），这些是高价值源
2. **去重 Anthropic engineering scraper**：原 `conoro` repo 404，要么找别人维护的 scraper，要么自己用 Firecrawl 监控 `/engineering`
3. **中文源备份方案**：5/7 中文 feed 不稳，aggregator 必须有 Firecrawl fallback 路径
4. **X 决策**：先确定走 X API 充值 vs RSSHub vs 只覆盖 blog 子集——这直接影响架构
5. 写 aggregator 代码前，把 `sources.opml` 里这 16 条死链清掉/替换，否则每次 cron run 都会喷错误日志
