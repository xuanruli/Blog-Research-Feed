---
name: brf-cli
description: Reference for the `brf` CLI used inside the Blog-Research-Feed agent container — the fetch-all / fetch-full pipeline, the FeedItem JSON schema, output file layout, jq triage recipes, and the auxiliary fetch/firecrawl/report subcommands. Use when composing brf commands, triaging /tmp/feed/index.json, or drilling into items.
---

# brf CLI

`brf` is pre-installed in the container (on PATH). Secrets auto-load from
`/workspace/.env` — never `source` it manually. Every command prints JSON
or a path to stdout, errors to stderr with a non-zero exit.

## Main pipeline (use these)

| Command | Effect | Output |
|---|---|---|
| `brf fetch-all --since YYYY-MM-DD [--output-dir DIR]` | Concurrent fetch of all configured sources (RSS / X / YouTube / Podcast / Firecrawl-index), dedupe by URL | writes `DIR/index.json` (default `/tmp/feed/`) + `DIR/full/<id>.*`; prints item count to stderr |
| `brf fetch-full --id ID [--output-dir DIR] [--force]` | Drill one item by id (HTML / transcript / Whisper / firecrawl scrape, dispatched by source_type) | writes `DIR/full/<id>.<ext>`, prints path; idempotent unless `--force` |
| `brf report slack --message-file PATH` | Post a markdown file to Slack (see the `slack-formatting` skill for what renders) | OK / error JSON; **call once, last** |

## FeedItem schema (every entry in index.json)

```json
{
  "id": "ab12cd34ef567890",       // sha1(source_type+url)[:16]; pass to fetch-full
  "source_type": "rss",           // rss | x | youtube | podcast | firecrawl_index
  "source": "Simon Willison's Weblog",
  "title": "...",
  "url": "https://...",
  "published": "2026-05-19T14:23:00+00:00",  // may be null (firecrawl_index common)
  "summary": "≤500 chars",
  "has_full": true,               // true → full/<id>.html already on disk (free to cat)
  "needs_firecrawl": false,       // true → fetch-full will firecrawl-scrape (costs)
  "extra": {}                     // source_type-specific (audio_url / channel_id / index_url ...)
}
```

### Drill-down cost ladder (cheapest first)
1. `has_full=true` → read `full/<id>.html` directly — zero cost
2. `has_full=false`, `needs_firecrawl=false` → summary already suffices
3. `needs_firecrawl=true` → `brf fetch-full` (firecrawl, ~$0.005)
4. `source_type=youtube|podcast` → `brf fetch-full` (captions free / Whisper $0.36/30min)

### Per-source-type notes
- `x`: `summary` is the full tweet; `has_full=true` but no extra drill needed.
- `firecrawl_index`: `summary` is empty by design (regex-extracted from index pages) — triage on title + source.
- `HF Daily Papers` (firecrawl_index): `published` is a **month-end timestamp** (arxiv `2605.xxxxx` → 2026-05-31). yymm is month-precision, not a bug.
- GitHub `releases.atom`: title is usually `pkg@x.y.z`, body often empty — low signal unless breaking change / new model / new capability.

## Reading index.json (it's ~150 KB / 300+ items)

`read` is capped ~110 KB per call → read in 2-3 chunks with offset, OR project with one jq pass then read:

```bash
# project to compact TSV for a single read
jq -r '.[] | "\(.id)\t\(.source_type)\t\(.source)\t\(.title)\t\(.summary[:200] // "")"' \
    /tmp/feed/index.json > /tmp/triage.tsv
```

Useful jq:
```bash
jq 'length' /tmp/feed/index.json                                   # total count
jq -r 'group_by(.source_type)|map({t:.[0].source_type,n:length})|.[]' /tmp/feed/index.json
jq -r '.[]|select(.id=="ID")|{url,summary,extra}' /tmp/feed/index.json   # inspect one
```

## Auxiliary commands (rarely needed)

```bash
brf fetch x-user --handle HANDLE --since YYYY-MM-DD      # an X account not in sources.yaml
brf fetch youtube-transcript --url URL
brf fetch podcast-transcript --url FEED_OR_EPISODE_URL
brf firecrawl scrape --url URL                           # arbitrary single-page fulltext
brf firecrawl search --query Q [--limit N]
```

## Failure handling

Any brf step failing (non-zero / `{"error": ...}` / `None`) → silently skip that
one item, never abort the whole run. `brf fetch-full` returning no path means the
content was unavailable (transcript disabled, paywall, etc.) — drop that item.
