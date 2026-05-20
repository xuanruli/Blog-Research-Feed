# `brf` Fetcher Architecture — Design Doc

**Status**: Proposed — under review
**Author**: collaborative session 2026-05-20
**Supersedes**: ad-hoc per-source-type CLI in `brf/main.py`

**Revision 2** (2026-05-20): address PR #10 review feedback + YouTube edge cases.
Changes since v1:
- §3.2 `id` formula includes `source_type` to avoid collisions across fetchers
- §3.3 ABC docstring documents the nested-pool concurrency contract
- §3.5 dedupe priority explicit, not "first-completed wins"
- §3.5 fetch_full default no-overwrite + `--force` flag
- §3.2 FeedItem gains `schema_version: "0.1"` from day one
- §3.4 YouTube fetcher: yt-dlp metadata-only fallback for empty descriptions; transcript-availability variance documented
- §5 SKIP list moved into `sources.yaml` (single source of truth)
- §7 cost estimate for Phase 4 (firecrawl_index)
- §10 Q3/Q4/Q5 closed (decisions made above); Q6 kept as known limitation

---

## 1. Context

Current `brf` exposes per-source-type CLI subcommands:

```
brf fetch rss --since DATE                    → only RSS (60 feeds from sources.opml)
brf fetch x-user --handle HANDLE --since DATE → one X user, manually invoked per handle
brf fetch youtube-transcript --url URL        → transcript only, after agent knows URL
brf fetch podcast-transcript --url URL        → transcript only, after agent knows URL
brf firecrawl scrape --url URL                → general-purpose URL scrape
```

After running this in production for several days against the curator
agent, three pain points became clear:

### Pain 1 — Coverage gaps

`sources.md` §5 lists ~30 no-feed sites (Anthropic news, OpenAI, DeepMind,
DeepSeek/Qwen/Moonshot blogs, leaderboards, etc.). They're not in
`sources.opml`. The agent has no systematic way to see them — they're
just dark. PR #8 adds a "FIRECRAWL_FALLBACK_FEEDS" lane for 3 specific
broken-RSS sites, but doesn't address the broader no-feed-site problem.

### Pain 2 — Agent's mental dispatch burden

The agent has to *know* which sources exist of each type, which call to
make for each, and how to merge results. Run #3's trace showed the agent
fanning out manually:

```bash
brf fetch rss --since YESTERDAY > /tmp/rss.json
brf fetch x-user --handle karpathy --since YESTERDAY > /tmp/x-karpathy.json &
brf fetch x-user --handle simonw --since YESTERDAY > /tmp/x-simonw.json &
brf fetch x-user --handle natolambert --since YESTERDAY > /tmp/x-natolambert.json &
wait
# ...then has to manually triage *each file separately*
```

The agent doesn't have a unified view of "what happened yesterday across
all sources." It works on partial views per type, which makes
cross-source comparison (e.g., "did anyone besides Karpathy talk about
this?") hard.

### Pain 3 — `full_text` bloats the bulk JSON

Current `brf fetch rss` returns items with `full_text` field that, for
FULL feeds, contains the entire article HTML (Karpathy 40K chars per
entry). Computed worst case:

| Feed class | Count × avg full_text size | Subtotal |
|---|---|---|
| FULL feeds (content:encoded present) | 35 × 10 items × 15K | ~5.25 MB |
| SUMMARY feeds | ~50 items × 0.8K | ~40 KB |
| **Total** | | **~5.3 MB ≈ 1.3M tokens** |

That exceeds Opus 4.7's 1M context window. The agent cannot ever read
`/tmp/rss.json` in full. Current workaround: agent uses `jq` projection
to extract just titles, then jq-selects to drill into specific items.
This works but:

- Title-only triage is information-poor — agent has to keyword-match
  ("Gemini 3.5|Mistral|joined Anthropic"), which misses good content
  with vague titles
- The full_text field is *vestigial* — it's there but agent can't read
  the JSON it's in
- Failure mode: if agent ever `cat`s the JSON or uses `read` tool, it
  crashes or truncates

### Pain 4 — Three RSS sub-variants with no consistent contract

A single OPML feed list contains feeds in three regimes:

- **FULL** (~35): `content:encoded` populated → full HTML body present
- **SUMMARY** (~9): `description` ≥ 80 chars but no full body
- **TITLE-ONLY** (~3 HN/qbitai/tldr): only `title`, summary is empty

Today's schema has `summary` and `full_text` fields, both can be empty
or null in various combinations. No documented contract for the agent on
"when do I need to scrape for full text vs. trust the summary."

---

## 2. Goals & non-goals

### Goals

1. **One bulk-fetch call** returns a unified, agent-readable JSON
   covering all source types in one pass.
2. **Unified `FeedItem` schema** with consistent semantics: `summary`
   always a string (never null), `has_full` explicit signal whether body
   is on disk, `needs_firecrawl` explicit signal whether summary is
   thin.
3. **Per-type drill-down** is uniform: `brf fetch-full --id ID` dispatches
   to the right fetcher (transcript for YouTube, Whisper for podcast,
   firecrawl for HTML, no-op for X tweets that are already complete).
4. **Light JSON index, separate full-body files**: agent reads the
   ~80KB index.json without context worries; deep-reads happen via
   `cat /tmp/feed/full/<id>.{html,txt,json}`.
5. **Extensibility**: adding a new source type (Reddit, Discord export,
   Bluesky, GitHub Discussions) is one new file + one config entry.
6. **No-feed sites covered**: §5 of sources.md is no longer dark. Each
   no-feed index is a `firecrawl_index` source.

### Non-goals

- Replacing existing low-level CLI (`brf fetch rss`, `brf fetch x-user`,
  etc.) — they stay for debugging / one-off use.
- Real-time / sub-daily feed pulls — daily granularity is enough.
- Caching layer — fetchers always pull fresh; if needed later, add at
  aggregator layer.
- Source-specific filtering beyond date — that's the agent's job at
  triage.

---

## 3. Proposed architecture

### 3.1 Class hierarchy

```
┌─────────────────────────────────────────────────────────────────┐
│  FeedItem (dataclass)        ← single normalized output schema  │
└─────────────────────────────────────────────────────────────────┘
        ↑ produced by
        │
┌───────┴─────────────────────────────────────────────────────────┐
│  SourceFetcher (ABC)                                            │
│    fetch(since)        → Iterable[FeedItem]    # bulk           │
│    fetch_full(item)    → bytes | None          # on-demand drill│
└─────────────────────────────────────────────────────────────────┘
        △
        │
  ┌─────┼──────┬───────────┬──────────┬──────────────────┐
RssFetcher  XFetcher  YouTubeFetcher  PodcastFetcher  FirecrawlIndexFetcher
("rss")    ("x")     ("youtube")     ("podcast")    ("firecrawl_index")
```

```
┌─────────────────────────────────────────────────────────────────┐
│  FeedAggregator                                                 │
│    fetch_all(since)    → concurrent run of all fetchers,        │
│                          dedupe by URL, write index.json +      │
│                          full/ files                            │
│    fetch_full(item_id) → look up source_type, dispatch to       │
│                          the right fetcher's fetch_full         │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 `FeedItem` — the normalized schema

```python
from dataclasses import dataclass, field
from typing import Literal

SourceType = Literal["rss", "x", "youtube", "podcast", "firecrawl_index"]

@dataclass
class FeedItem:
    """One item the agent sees in index.json. Schema is uniform across
    source types so triage logic doesn't need per-type branching."""

    schema_version: str              # always "0.1" today; bump on breaking change
    id: str                          # sha1(f"{source_type}:{url}")[:16] — see §3.2.1
    source_type: SourceType
    source: str                      # display name: "Karpathy", "@karpathy", "Matt Pocock"
    title: str                       # may be "" for X tweets
    url: str                         # canonical link
    published: str | None            # ISO 8601 UTC, None if unknown
    summary: str                     # ≤500 chars plain text, "" allowed
    has_full: bool                   # /tmp/feed/full/<id>.* exists (pre-fetched)
    needs_firecrawl: bool            # summary thin; agent should consider scraping
    extra: dict = field(default_factory=dict)
    # extra type-specific fields go here so they don't pollute the
    # top-level schema:
    #   x:          {like_count, retweet_count, ...}
    #   youtube:    {duration_seconds, channel_id, transcript_source}
    #   podcast:    {episode_index, audio_url, duration_seconds}
```

### 3.2.1 `id` formula — collision avoidance

The same URL can legitimately surface through multiple fetchers (e.g., an
Anthropic news post might be discovered by both `RssFetcher` reading
`anthropic.com/engineering` AND `FirecrawlIndexFetcher` scraping
`anthropic.com/news`). Hashing only on URL would assign them the same
`id`, and the dedupe step (§3.5) would silently drop one — but if we
later allow same-URL items to coexist (e.g., one with transcript, one
without), we'd lose a degree of freedom.

```python
id = hashlib.sha1(f"{source_type}:{url}".encode()).hexdigest()[:16]
```

Length bumped to 16 (from 12) to keep collision probability negligible
even with `source_type:url` keying. Birthday-paradox: ~2^32 items before
a 1% collision chance; we'll never hit that.

**Why dataclass not class hierarchy**: agent reads JSON, not Python
objects. A uniform shape simplifies serialization, jq queries, and the
agent's mental model. Type-specific fields live in `extra: dict` so they
don't bloat the top-level schema for everyone.

**`summary` always-string contract**: agent code can assume non-None.
For title-only feeds, `summary == ""` and `needs_firecrawl=True` signals
the agent to scrape if interested.

**`schema_version` from day one**: cheap forward-compat. When we
inevitably break the schema in 6 months, migration code can sniff this
field instead of guessing.

### 3.3 `SourceFetcher` ABC

```python
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime

class SourceFetcher(ABC):
    """Base for per-type source fetchers.

    Concurrency contract: each fetcher owns its internal parallelism
    (e.g., RssFetcher uses ThreadPoolExecutor over the feed list,
    XFetcher fans out per handle). The Aggregator's job is only to run
    different fetchers in parallel — NOT to manage workers inside any
    one fetcher. This nested-pool model means a typical run is:

        aggregator pool (5 fetchers in parallel)
          ├── RssFetcher (10 workers, one per OPML feed)
          ├── XFetcher (5 workers, one per handle, capped for rate limit)
          ├── YouTubeFetcher (10 workers, one per channel RSS)
          ├── PodcastFetcher (10 workers, one per podcast RSS)
          └── FirecrawlIndexFetcher (sequential — firecrawl rate limit)

    Total ~40-45 threads at peak. Negligible for our scale.
    """

    source_type: str  # class attribute set by subclass

    @abstractmethod
    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Bulk pull. Implementation owns its internal concurrency."""

    @abstractmethod
    def fetch_full(self, item: FeedItem) -> bytes | None:
        """On-demand drill: get the full body / transcript / etc. Returns
        bytes so the aggregator can write to a file with the appropriate
        extension. Returns None if unavailable (not an exception)."""
```

### 3.4 Per-fetcher contracts

| Fetcher | bulk `fetch` | drill-down `fetch_full` | Storage on disk |
|---|---|---|---|
| `RssFetcher` | Parse OPML, parallel httpx, per-entry normalize via 3-branch logic | Firecrawl scrape the URL | `full/<id>.html` (only when content:encoded was present) |
| `XFetcher` | Parallel X API per handle (from config), `exclude=retweets,replies` | No-op (tweet text already in summary). Future: thread context | None — X is summary-complete |
| `YouTubeFetcher` | Parse channel RSS, extract title/url/description | `youtube-transcript-api`, fallback yt-dlp + Whisper | `full/<id>.txt` (transcript) |
| `PodcastFetcher` | Parse podcast RSS, extract title/url/showNotes | Download mp3, Whisper transcribe | `full/<id>.txt` (transcript) |
| `FirecrawlIndexFetcher` | Firecrawl scrape index page, regex-extract article links | Firecrawl scrape the article URL | `full/<id>.md` |

#### Why RSS variants stay inside RssFetcher

The variant (full / summary / title-only) is determined **per entry**,
not per feed — a single feed can mix them. So a single
`_normalize_entry()` with branching is cleaner than three RssFetcher
subclasses:

```python
def _normalize_entry(self, entry, feed_meta) -> FeedItem:
    content_encoded = entry.get("content:encoded") or ""
    description = entry.get("description") or entry.get("summary") or ""

    if content_encoded:
        plain = _strip_html(content_encoded)
        summary = _truncate(plain, 500)
        has_full = True
        needs_firecrawl = False
        self._save_full(item_id, content_encoded)
    elif description and len(_strip_html(description)) >= 80:
        summary = _truncate(_strip_html(description), 500)
        has_full = False
        needs_firecrawl = False
    else:
        summary = ""
        has_full = False
        needs_firecrawl = True

    return FeedItem(...)
```

#### Why YouTube / Podcast are NOT lumped under RssFetcher

Both technically come from RSS feeds, but their drill-down behavior is
completely different (transcript pipelines, not HTML scraping). Lumping
would require `RssFetcher.fetch_full` to branch on URL pattern, which
re-introduces the dispatch problem we're trying to solve. Separate
classes keep per-type behavior cohesive.

#### YouTubeFetcher edge cases

**(a) Empty `media:description`.** YouTube channel RSS feeds frequently
ship empty descriptions, especially for tutorial videos or shorts. RSS
title alone is often too thin to triage ("Build it with Claude #14").
Strategy: try yt-dlp metadata-only fallback (free, no API key, just an
HTTP scrape of the watch page):

```python
def _normalize_youtube_entry(self, entry):
    desc = (entry.get("media:description") or "").strip()
    if len(desc) < 80:
        # yt-dlp metadata-only: extract_info(url, download=False)
        # Costs 1 HTTP request to youtube.com per call. No API key needed.
        meta = self._ytdlp_metadata(entry["url"])
        if meta and meta.get("description"):
            desc = meta["description"]
    return FeedItem(
        source_type="youtube",
        title=entry["title"],
        summary=_truncate(desc, 500),       # may still be "" if both fail
        has_full=False,                      # transcript not fetched yet
        needs_firecrawl=False,               # firecrawl on YT page is useless
        extra={
            "channel_id": entry["channel_id"],
            "duration_seconds": meta.get("duration") if meta else None,
        },
    )
```

**`needs_firecrawl=False` is deliberate for YouTube items**: scraping a
youtube.com URL via Firecrawl returns the React shell (no transcript, no
useful description). Agent should dispatch by `source_type=youtube` →
`fetch-full` (transcript path) rather than firecrawl.

**(b) Transcript availability variance.** Some videos have captions,
some don't (creator disabled them; cloud egress IP banned from
caption API; original language not English; etc). PR #8 already
implemented two-leg fallback in `brf/youtube.py:get_transcript()`:

```
fetch_full(youtube_item):
    1. youtube-transcript-api  (free, IP-banned on cloud is common)
    2. If 1 fails: yt-dlp bestaudio (≤25MB) + OpenAI Whisper transcribe
                   ($0.006/min; ~$0.18 for a 30-min talk)
    Returns transcript bytes + sets extra["transcript_source"] ∈ {captions, whisper}
    Returns None if both legs fail (private/deleted video, or Whisper >25MB)
```

**Cost protection**: system_prompt limits ≤ 2 video/podcast transcripts
per day. Whisper has a 25MB-per-file hard limit which caps any single
call at ~70min of audio. Long videos that exceed this fail loudly
(`extra["transcript_source"] = null`, error logged) rather than running
up costs.

**(c) Channel discovery is yaml-driven**, not autodiscovery. `sources.yaml`
lists explicit `channel_id`s. Adding a channel = one yaml line.

### 3.5 `FeedAggregator`

```python
class FeedAggregator:
    def __init__(self, fetchers: list[SourceFetcher], output_dir: Path):
        self.fetchers = fetchers
        self.by_type = {f.source_type: f for f in fetchers}
        self.output_dir = output_dir
        (self.output_dir / "full").mkdir(parents=True, exist_ok=True)

    # Dedupe priority: when the same URL surfaces from multiple
    # fetchers, the higher-priority source wins. Rationale:
    # - rss has the richest metadata (published date, content:encoded)
    # - youtube/podcast carry duration + transcript hint
    # - x is summary-complete but no title
    # - firecrawl_index is the weakest (extracted from regex on HTML,
    #   may have wrong title or stale date) — only useful when nothing
    #   else covers the source.
    _DEDUPE_PRIORITY = {
        "rss": 0,
        "youtube": 1,
        "podcast": 2,
        "x": 3,
        "firecrawl_index": 4,
    }

    def fetch_all(self, since: datetime) -> list[FeedItem]:
        """Run all fetchers concurrently. Write index.json + full/ files.
        Return the (deduped) item list."""
        all_items: list[FeedItem] = []
        with ThreadPoolExecutor(max_workers=len(self.fetchers)) as pool:
            futures = {
                pool.submit(list, f.fetch(since)): f.source_type
                for f in self.fetchers
            }
            for fut in as_completed(futures):
                try:
                    all_items.extend(fut.result())
                except Exception as exc:
                    print(f"[aggregator] {futures[fut]} failed: {exc}",
                          file=sys.stderr)

        # Dedupe by URL with explicit priority (NOT first-completed).
        # If RSS and firecrawl_index both produce the same Anthropic
        # news URL, RSS wins because its metadata is richer.
        seen: dict[str, FeedItem] = {}
        for it in all_items:
            existing = seen.get(it.url)
            if existing is None:
                seen[it.url] = it
            elif self._DEDUPE_PRIORITY[it.source_type] < self._DEDUPE_PRIORITY[existing.source_type]:
                seen[it.url] = it
        unique = list(seen.values())

        # Write index.json
        index_path = self.output_dir / "index.json"
        index_path.write_text(
            json.dumps([asdict(it) for it in unique],
                       ensure_ascii=False, indent=2)
        )
        return unique

    def fetch_full(self, item_id: str, force: bool = False) -> Path | None:
        """Look up item, dispatch to right fetcher, write to full/.

        Default behavior is idempotent: if `full/<id>.{ext}` already
        exists, return it without re-fetching (no API cost). Pass
        force=True to overwrite and re-fetch.
        """
        item = self._load_item(item_id)
        if item is None:
            return None
        ext = self._extension_for(item.source_type)
        path = self.output_dir / "full" / f"{item.id}.{ext}"
        if path.exists() and not force:
            return path  # cached, no API call
        fetcher = self.by_type[item.source_type]
        content = fetcher.fetch_full(item)
        if content is None:
            return None
        path.write_bytes(content)
        return path
```

---

## 4. CLI surface

```bash
# Bulk: agent's primary entry point
brf fetch-all --since 2026-05-19 [--output-dir /tmp/feed]
# → writes /tmp/feed/index.json + /tmp/feed/full/<id>.* (for pre-fetched items)
# → prints "N items written to /tmp/feed/index.json" to stderr

# Drill-down: by id (looks up source_type internally).
# Default is idempotent: if full/<id>.{ext} already exists, returns
# the cached path WITHOUT re-fetching. Use --force to re-fetch.
brf fetch-full --id abc123 [--output-dir /tmp/feed] [--force]
# → writes /tmp/feed/full/abc123.{html,txt,md,json}
# → prints the written path to stdout

# Lower-level commands stay for debugging / one-off:
brf fetch rss --since DATE            # only RSS (legacy)
brf fetch x-user --handle HANDLE      # one handle (legacy)
brf fetch youtube-transcript --url U  # raw transcript fetch
brf firecrawl scrape --url U          # raw scrape
brf report slack --message-file FILE  # unchanged
```

### Agent workflow

```bash
# 1. One bulk call
brf fetch-all --since "$YESTERDAY"

# 2. Read the full index — fits in context (~80KB)
cat /tmp/feed/index.json

# 3. Triage: agent reads all summaries, picks 10 standouts
# (in its head; no jq needed for triage)

# 4. Per pick, dispatch by has_full + needs_firecrawl + source_type:
#    case A: has_full=true → cat /tmp/feed/full/<id>.html   (free)
#    case B: has_full=false → brf fetch-full --id <id>       (costs)

# 5. Compose report from accumulated info; post.
brf report slack --message-file /tmp/report.md
```

---

## 5. Configuration: `sources.yaml`

Replaces `sources.opml` + scattered prose in `sources.md` with a single
structured config the aggregator reads at startup:

```yaml
# brf/sources.yaml — bundled inside the pip package
#
# Single source of truth: skip-list / firecrawl-fallback / summary-only
# all live as flags on individual entries, not as separate hardcoded
# sets in Python. Adding a skip / fallback no longer requires a code
# change + brf bump — just yaml + env recreate.

rss:
  - { name: "Karpathy",        url: "https://karpathy.bearblog.dev/feed/" }
  - { name: "Hamel",           url: "https://hamel.dev/index.xml" }
  - { name: "Interconnects",   url: "https://www.interconnects.ai/feed" }
  - { name: "vLLM releases",   url: "https://github.com/vllm-project/vllm/releases.atom" }

  # Skip entries: still listed for visibility, but marked disabled so
  # `RssFetcher` filters them out at boot. Saves operators from grepping
  # both yaml and Python to know what's covered.
  - { name: "Reddit r/LocalLLaMA",  url: "https://www.reddit.com/r/LocalLLaMA/.rss", enabled: false, reason: "403 — Reddit blocks cloud UA" }
  - { name: "jxnl.co",              url: "https://jxnl.co/feeds/feed.xml",            enabled: false, reason: "404 since site redesign" }

  # Summary-only feeds: flag so agent's needs_firecrawl signal is set
  # without RssFetcher needing a separate SUMMARY_ONLY_FEEDS constant.
  - { name: "Eugene Yan",      url: "https://eugeneyan.com/rss/",                      summary_only: true }
  - { name: "Hacker News",     url: "https://news.ycombinator.com/rss",                summary_only: true }
  # ... 60 entries total

x:
  handles:
    - karpathy
    - simonw
    - natolambert
    - sleepinyourhat
    - AmandaAskell
    # core ~10; sources.md §5 has 45 listed — agent picks others on demand
    # via `brf fetch x-user --handle X` (legacy CLI)

youtube:
  channels:
    - { name: "Matt Pocock",     id: "UCswG6FSbgZjbWtdf_hMLaow" }
    - { name: "Karpathy",        id: "UCXUPKJO5MZQN11PqgIvyuvQ" }
    - { name: "Yannic Kilcher",  id: "UCZHmQk67mSJgfCCTn7xBfew" }
    # ...

podcasts:
  - { name: "Latent Space",         url: "https://api.substack.com/feed/podcast/1084089.rss" }
  - { name: "Cognitive Revolution", url: "https://feeds.simplecast.com/...new-id..." }
  # ... (replace 3 dead podcast URLs from SOURCES_HEALTH.md §1)

firecrawl_index:
  - name: "Anthropic News"
    url: "https://www.anthropic.com/news"
    article_url_regex: 'https?://www\.anthropic\.com/news/[a-z0-9-]+'
    date_format: null      # no date in URL
    date_group: null
  - name: "OpenAI News"
    url: "https://openai.com/news"
    article_url_regex: 'https?://openai\.com/(?:index/)?[a-z0-9-]+'
    date_format: null
    date_group: null
  - name: "DeepSeek News"
    url: "https://api-docs.deepseek.com/news"
    article_url_regex: '...'
    date_format: null
    date_group: null
  - name: "HF Daily Papers"
    url: "https://huggingface.co/papers"
    article_url_regex: 'https?://huggingface\.co/papers/(\d{4})\.\d{4,5}'
    date_format: "yymm"
    date_group: 1
  - name: "机器之心"
    url: "https://www.jiqizhixin.com"
    article_url_regex: 'https?://www\.jiqizhixin\.com/articles/(\d{4}-\d{2}-\d{2})-\d+'
    date_format: "%Y-%m-%d"
    date_group: 1
  - name: "LangChain Blog"
    url: "https://blog.langchain.com"
    article_url_regex: ...
    date_format: null
    date_group: null
  # ~30 entries covering §5 of sources.md
```

Aggregator builder reads this:

```python
def build_aggregator(output_dir: Path) -> FeedAggregator:
    cfg = yaml.safe_load(importlib.resources.files("brf").joinpath("sources.yaml").read_text())
    fetchers: list[SourceFetcher] = [
        RssFetcher(cfg["rss"], output_dir=output_dir),
        XFetcher(cfg["x"]["handles"]),
        YouTubeFetcher(cfg["youtube"]["channels"]),
        PodcastFetcher(cfg["podcasts"]),
        FirecrawlIndexFetcher(cfg["firecrawl_index"]),
    ]
    return FeedAggregator(fetchers, output_dir=output_dir)
```

---

## 6. File layout

```
brf/
├── __init__.py
├── __main__.py
├── main.py                  ← CLI; adds fetch-all + fetch-full subcommands
├── config.py                ← .env loading (unchanged)
├── feed_item.py             ← FeedItem dataclass
├── aggregator.py            ← FeedAggregator
├── fetchers/
│   ├── __init__.py
│   ├── base.py              ← SourceFetcher ABC
│   ├── rss.py               ← RssFetcher (replaces brf/rss.py logic)
│   ├── x.py                 ← XFetcher (wraps brf/x_client.py)
│   ├── youtube.py           ← YouTubeFetcher (wraps brf/youtube.py)
│   ├── podcast.py           ← PodcastFetcher (wraps brf/podcast.py)
│   └── firecrawl_index.py   ← FirecrawlIndexFetcher
├── sources.yaml             ← NEW: structured source config (bundled in wheel)
├── sources.opml             ← KEEP for human readers / Feedly import (unchanged)
├── rss.py                   ← KEEP for `brf fetch rss` legacy CLI (low-level)
├── x_client.py              ← KEEP, used by fetchers/x.py
├── youtube.py               ← KEEP, used by fetchers/youtube.py
├── podcast.py               ← KEEP, used by fetchers/podcast.py
├── firecrawl_client.py      ← KEEP, used by fetchers/rss.py + firecrawl_index.py
├── slack.py
└── io.py
```

---

## 7. Migration & deployment plan

This is too big to ship in one PR. Phased rollout:

### Phase 1: schema + ABC + scaffolding (no behavior change)

- Add `feed_item.py`, `fetchers/base.py`, `aggregator.py` (skeleton)
- Add `sources.yaml` parser
- All old CLI subcommands keep working unchanged
- Tests: unit-test `FeedItem` serialization round-trip
- Ship as `brf 0.2.0`, env-v7

### Phase 2: RSS migration

- Refactor existing `brf/rss.py` logic into `fetchers/rss.py:RssFetcher`
- `brf fetch rss` (legacy) keeps working, delegates to `RssFetcher.fetch`
- Index.json + full/ file output for RSS-only items
- Ship as `brf 0.2.1`, env-v8

### Phase 3: per-type fetchers

- `fetchers/x.py` wraps `x_client.py` — `brf fetch-all` includes X items
- `fetchers/youtube.py` + `fetchers/podcast.py` similarly
- Ship as `brf 0.2.2`, env-v9

### Phase 4: `firecrawl_index`

- New `fetchers/firecrawl_index.py`
- ~30 no-feed sites configured in `sources.yaml`
- Ship as `brf 0.2.3`, env-v10

#### Cost estimate (matters because firecrawl is the only paid leg here)

Per daily run:

| Operation | Count/day | Unit cost | Daily cost |
|---|---|---|---|
| index scrape (one per no-feed site) | 30 | $0.005 | $0.15 |
| article scrape (agent drills down via `fetch-full`) | ~10-15 | $0.005 | $0.05-0.08 |
| **Phase 4 firecrawl_index subtotal** | | | **~$0.20-0.25/day** |

Plus existing (Phase 2 already shipped):
| RSS firecrawl fallback (3 sites, jiqizhixin/HF/langchain) | 3 | $0.005 | $0.015 |
| RSS summary-only firecrawl drill (~5/day) | 5 | $0.005 | $0.025 |

**Total firecrawl spend per daily run: ~$0.25/day ≈ $7-8/month.**

Plus Whisper (transcripts, ≤2/day cap): $0.36/day worst case ≈ $11/month.
Plus X API (10 handles, 2 reads each): $0.10/day ≈ $3/month.

**All-in cost target: <$25/month for daily curator runs.** If
firecrawl_index list grows (e.g., to 60 no-feed sites), recompute and
consider per-site cache or weekly-instead-of-daily for less critical
sources.

### Phase 5: agent system prompt rewrite

- Teach agent the new `brf fetch-all` + `brf fetch-full` flow
- Drop the per-type fanout examples
- Keep legacy CLI examples as escape hatch
- Ship as agent config update (no brf bump)

Total cumulative deploy: 4 PyPI releases, 4 env recreates. Each phase
deployable independently — if phase 2 breaks, we can hold there and
debug without touching anything else.

---

## 8. Alternatives considered

### Alt A: Stay with current per-type CLI

**Pros**: zero work.

**Cons**: Pain points 1-4 all unresolved. Pain 1 (coverage gaps) means
30 important sources will stay dark indefinitely. Pain 3 (5MB JSON) means
agent always uses jq projection workaround, which is fragile.

**Rejected.**

### Alt B: Class hierarchy on FeedItem (per-type subclass)

```python
class FeedItem(ABC): ...
class RssItem(FeedItem): ...
class XItem(FeedItem): ...
```

**Pros**: type safety in Python, IDE autocomplete.

**Cons**: When serialized to JSON, the agent sees only fields anyway —
the hierarchy adds no value for the agent. It adds complexity in:

- JSON deserialization (needs dispatch on source_type to pick subclass)
- Cross-type filtering (agent does `select(.source_type=="x")` in jq,
  doesn't care that XItem has different Python methods)
- Adding new types requires both a fetcher AND an item subclass

**Rejected.** `dataclass` + `extra: dict` is the right level of
abstraction for our use case. The hierarchy is only in fetchers, where
behavior actually varies.

### Alt C: Strategy pattern instead of inheritance

```python
class SourceFetcher:
    def __init__(self, fetch_strategy, normalize_strategy, drill_strategy):
        ...
```

**Pros**: more flexible composition than inheritance.

**Cons**: Overkill for ~5-7 source types. Strategy hierarchies pay off
when you have orthogonal axes of variation; ours is just one axis (source
type). ABC + subclass is simpler and Pythonic.

**Rejected.**

### Alt D: Skip `sources.yaml`, keep `sources.opml`

**Pros**: no new config file.

**Cons**: OPML can't represent X handles, YouTube channel IDs, or
firecrawl index regexes cleanly. We'd either parse them out of OPML
extension attributes (ugly) or have a second config file anyway. Better
to consolidate.

**Rejected.** sources.opml stays as a human-readable export (and Feedly
import format), but `sources.yaml` is the new source of truth for `brf`.

### Alt E: Drop `full_text` entirely from the index, always firecrawl

**Pros**: schema simpler — no `has_full` flag needed.

**Cons**: For FULL RSS feeds (the majority by item count), we already
have the full body via `content:encoded`. Throwing it away and
re-firecrawling costs $0.005 per article × ~350 items/day = $1.75/day
unnecessarily.

**Rejected.** Pre-fetch when we can (RSS content:encoded), defer when we
must (HTML scrape, transcript).

### Alt F: Single mega-class with type discrimination

```python
class FetcherBag:
    def fetch_rss(self, ...): ...
    def fetch_x(self, ...): ...
    def fetch_youtube(self, ...): ...
```

**Pros**: One file, no dispatch.

**Cons**: Violates SRP. Adding a new type means editing the mega-class
and risking regressions in unrelated methods. Testing requires
instantiating the whole bag. Extension by external code (third-party
fetcher) requires monkey-patching.

**Rejected.**

---

## 9. Extensibility examples

### Adding Reddit (new source type)

1. New file `brf/fetchers/reddit.py`:

   ```python
   class RedditFetcher(SourceFetcher):
       source_type = "reddit"

       def __init__(self, subreddits: list[str]):
           self.subreddits = subreddits

       def fetch(self, since):
           # Reddit JSON API
           for sub in self.subreddits:
               for post in self._fetch_subreddit(sub, since):
                   yield FeedItem(
                       id=...,
                       source_type=self.source_type,
                       source=f"r/{sub}",
                       title=post["title"],
                       url=post["url"],
                       published=post["created_at"],
                       summary=post["selftext"][:500],
                       has_full=True,  # selftext is the post
                       needs_firecrawl=False,
                       extra={"upvotes": post["ups"], "comments": post["num_comments"]},
                   )

       def fetch_full(self, item):
           # Top comments
           ...
   ```

2. Add config in `sources.yaml`:

   ```yaml
   reddit:
     - r/LocalLLaMA
     - r/MachineLearning
   ```

3. One line in aggregator builder:

   ```python
   fetchers.append(RedditFetcher(cfg["reddit"]))
   ```

**Total diff**: 1 new file + 2 line changes. Zero touches to existing
fetchers or to FeedItem.

### Adding a per-item enrichment (e.g., sentiment score)

The `extra` dict is free-form. A future enrichment can post-process the
aggregator's output, adding fields per item:

```python
items = aggregator.fetch_all(since)
for it in items:
    it.extra["sentiment"] = sentiment_model.score(it.summary)
```

The agent's index.json now has `extra.sentiment` per item — no schema
change required.

---

## 10. Open questions

1. **X handle list scope** — RESOLVED: bulk-fetch core ~10 in
   `sources.yaml`; remaining ~35 documented in system prompt as
   "additional handles you can ask about" via legacy `brf fetch x-user`.

2. **`fetch_full` for X** — RESOLVED: no-op for v1. Tweets are
   summary-complete. If thread context becomes important, add separate
   `brf fetch x-thread --tweet-id` command in a future phase.

3. **Concurrency limits** — RESOLVED in §3.3 ABC docstring: each
   fetcher owns internal parallelism (RSS 10, X 5, YouTube 10, Podcast
   10, Firecrawl sequential). Aggregator only parallelizes across
   fetchers (5 outer workers). Total ~40-45 threads at peak.

4. **`fetch_full` overwrite semantics** — RESOLVED in §3.5: default
   idempotent (returns cached path if file exists, no API call),
   `--force` flag to re-fetch.

5. **Schema versioning** — RESOLVED in §3.2: `schema_version: "0.1"`
   from day one. Cheap forward-compat.

6. **Date precision for `firecrawl_index`** — known limitation, Phase 4.
   `FALLBACK_MAX_ITEMS = 10` cap is the v1 mitigation. Improving date
   precision would require a secondary firecrawl per article (~$0.005 ×
   N article extras/day) which is not worth it pre-launch. Revisit if
   stale-content complaints surface in production.

---

## 11. Approval checklist

Before starting Phase 1 implementation:

- [x] FeedItem schema reviewed and approved (esp. `extra: dict` semantics)
- [x] Fetcher ABC interface reviewed (`fetch_full → bytes | None` ok?)
- [x] sources.yaml schema reviewed — skip-list/summary-only flags
      consolidated into yaml per review #4
- [x] File layout reviewed — legacy `brf/rss.py` retention deferred to
      Phase 2 per review #7
- [x] Phased migration plan approved (5 phases ≈ 5 brf releases)
- [x] Open questions §10 — Q3, Q4, Q5 resolved in this revision;
      Q1, Q2 answered (above); Q6 deferred as known Phase 4 limitation

Phase 1 ready to implement.

## 12. Deferred items (Phase 2+)

Tracked here so they don't get lost:

- **Review #7 — legacy `brf/rss.py` consolidation.** After Phase 1
  stabilizes, point the legacy `brf fetch rss` CLI at
  `fetchers/rss.py:RssFetcher` and delete the old module to eliminate
  two-source-of-truth drift. Phase 2 deliverable.

- **YouTube duration pre-check.** Before `yt-dlp download`, call
  `extract_info(url, download=False)` to read `duration_seconds`; skip
  if it would exceed Whisper 25MB. Saves bandwidth on long videos. Cheap
  to add to `YouTubeFetcher.fetch_full`. Phase 3 deliverable.

- **firecrawl_index article date precision.** Optional secondary
  firecrawl on each article URL to extract real publish date (instead of
  trusting URL slug heuristics). Defer until staleness becomes a real
  agent-side complaint.

- **Per-handle X recent search endpoint** (`from:A OR from:B`). Could
  reduce X API spend by ~5× if agent traffic grows beyond ~10
  handles/day. Pre-mature now.
