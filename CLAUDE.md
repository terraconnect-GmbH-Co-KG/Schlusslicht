# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Schlusslicht** is a German satirical news site focused on "last-place" topics — the worst performers, failures, and decline across 24 categories. The site is fully static HTML, rebuilt daily via a Python AI pipeline that calls the Anthropic API with web search to research and inject fresh content.

## Running the Generator

```bash
pip install requests beautifulsoup4
ANTHROPIC_API_KEY="your-key" python generate.py
```

This reads `index.template.html` (falls back to `index.html`), calls the Anthropic API to research all content, then writes the result to `index.html`. The previous `index.html` is preserved if the API call fails.

## Architecture

The entire build is a single script: `generate.py`. It:

1. **Fetches 24 daily items** (`get_daily_items()`) — one per rubric (sport, space, corruption, climate, etc.), each searched via the `web_search_20250305` tool (up to 30 uses per call).
2. **Fetches 3 background stories** (`get_daily_stories()`) and additional ticker/spotlight content.
3. **Injects into HTML** (`inject()`) using BeautifulSoup CSS selectors:
   - Rubric cards: `[data-rubrik="01"]` through `[data-rubrik="24"]`
   - Story cards: `.story-card` (nth-of-type)
   - Ticker: `#ta-hl`, `#ta-text`
   - Spotlight: dedicated ID selectors

All client-side API calls in the HTML are disabled via regex replacements during the build — the output `index.html` is fully self-contained with no API keys.

## Key Configuration (top of generate.py)

| Variable | Value |
|----------|-------|
| `MODEL` | `claude-sonnet-4-20250514` |
| `API_URL` | `https://api.anthropic.com/v1/messages` |
| `TEMPLATE` | `index.template.html` (or `index.html` fallback) |
| `OUTPUT` | `index.html` |
| Timeout | 240 seconds per API call |
| Retry | 3 attempts, 6 s exponential backoff |

## Automation

`daily-update.yml` runs the generator via GitHub Actions daily at 04:10 UTC, then commits and pushes the updated `index.html`. Required secret: `ANTHROPIC_API_KEY`. Workflow needs `contents: write` permission. See `SETUP-github-actions.md` for full deployment instructions (German).

## The 24 Rubrics

Defined as a list in `get_daily_items()`. Each entry has an id (`"01"`–`"24"`), a topic label, and a system prompt fragment. Changing a rubric's content or search focus means editing this list — the HTML template's `data-rubrik` attributes must match the ids.
