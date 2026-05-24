---
name: slack-formatting
description: How to author the report markdown so it renders correctly after `brf report slack` converts it to Slack mrkdwn. Slack's mrkdwn is NOT CommonMark. Use when writing /tmp/draft.md or any message destined for `brf report slack`.
---

# Slack formatting for `brf report slack`

You write **standard markdown**; `brf report slack` runs a converter
(`brf/slack.py:_markdown_to_mrkdwn`) that rewrites it to Slack mrkdwn and
splits it into Block Kit section blocks on H1/H2 boundaries (max ~2900
chars/section). Write for that converter — these are its exact rules.

## What the converter handles (write these)

| You write | Becomes in Slack | Notes |
|---|---|---|
| `**bold**` | `*bold*` | single line only — `**` spanning a newline won't convert |
| `[label](url)` | `<url\|label>` | the correct Slack link form; never write raw `<url\|label>` yourself |
| `# H1` … `###### H6` | `*heading*` (bold line) | Slack has no real headings; all become bold |
| `- item` / `* item` | `• item` | bullets become bullet dots |
| `` `code` `` | `` `code` `` | passthrough — Slack supports inline code |
| ` ```block``` ` | ` ```block``` ` | passthrough — Slack supports code fences |
| `> quote` | `> quote` | passthrough — Slack supports blockquote |

**Section splitting**: H1/H2 (`#` / `##`) start a new Slack block. Use `##`
for your top-level report sections (🎯 Top 10 / 🔬 Research / 🚀 Releases)
so each becomes its own block. `###` for per-item headers stays inside a block.

## What does NOT work (avoid)

- **Single-asterisk italic `*italic*`** — Slack reads single `*` as **bold**, so it renders bold, not italic. Don't use `*x*` for emphasis. Use `_x_` if you truly need italic (Slack-native), but the converter won't help — prefer just bold or plain.
- **`~~strikethrough~~`** — not converted; Slack's is single-tilde `~x~`. Markdown `~~x~~` renders literally. Avoid.
- **Markdown tables** (`| a | b |`) — Slack has no table support; they render as ugly literal pipes. Use bullet lists instead.
- **Images `![alt](url)`** — not rendered as images in a webhook section block; the `[..](..)` part becomes a link. Don't embed images.
- **Nested / multi-line bold** — the bold regex is single-line; `**` across a line break stays literal.
- **Deeply nested bullets** — Slack flattens indentation; keep lists one level.

## Authoring guidance

- Short paragraphs + `-` bullets read best in Slack.
- Keep each `##` section under ~2900 chars or the converter hard-splits it mid-content into a second block (ugly breaks). For a long Top-10, that's fine since each item is small, but don't dump a 4000-char wall in one section.
- Links: always `[text](https://...)`, never bare URLs in angle brackets.
- Don't write `@here` / `@channel` / raw `@name` — they won't resolve and may annoy.

## Quick example

```markdown
## 🎯 Top 10 ranked signal

### 1. [Multimodal] **SAM 3.1: real-time video tracking**

**为什么有用**：one-line takeaway.

**核心点**：
- bullet with a [link](https://ai.meta.com/blog/segment-anything-model-3/)
- another bullet

**来源**：Meta AI Blog
```

renders in Slack as a section block with a bold heading line, a bold
"🎯 Top 10..." block header, bold sub-headers, `•` bullets, and an inline
`<url|link>`.
