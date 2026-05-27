# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Schlusslicht** is a German satirical news site ("Das Magazin der Letzten") covering last-place performers and failures across 24 categories. The site is fully static HTML, rebuilt daily by a Python pipeline that calls the OpenRouter API with a web search server tool to research and inject fresh content.

## Running the Generator

```bash
pip install requests beautifulsoup4
OPENROUTER_API_KEY="your-key" python rebuild/generate.py
```

Reads `index.template.html` (falls back to `index.html`), calls OpenRouter with web search, writes the result to `index.html`. The previous `index.html` is left untouched if all API calls fail.

## Architecture

The entire build is a single script: **`rebuild/generate.py`**. It makes exactly two API calls:

1. **`get_daily_items()`** — fetches all 24 rubric cards, the spotlight section, and the 8 ticker items in one call (returns a single JSON object).
2. **`get_daily_stories()`** — fetches 3 long-form background stories in one call.

Both calls use `{"type": "openrouter:web_search"}` as a server-side tool — OpenRouter handles the search autonomously, no tool-result loop required. The response is OpenAI-compatible: `choices[0].message.content`.

`inject()` then writes everything into the HTML using BeautifulSoup CSS selectors:

| Content | Selector |
|---|---|
| Rubric cards | `[data-rubrik="01"]` … `[data-rubrik="24"]` |
| Rubric headline | `.rtit` (text after ` — ` is replaced) |
| Rubric comment | `.realsatire` |
| Rubric source tag | `.ai-tag` |
| Spotlight | `#ta-cat`, `#ta-hl`, `#ta-text`, `#ta-source` |
| Ticker items | `.ticker-inner` (rebuilt, doubled for seamless scroll) |
| Story preview cards | `.story-card` (first 3, nth by index) |
| Story modals | `#story1`, `#story2`, `#story3` |
| Build timestamp | `#nav-issue-label`, `#update-time` |

After injection, two regex replacements disable client-side API calls in the HTML so the output is fully self-contained:
```python
re.sub(r"loadDailyContent\(\)\s*,", "Promise.resolve(),", out)
re.sub(r"loadDailyStories\(\)\s*,", "Promise.resolve(),", out)
```

## Key Configuration (`rebuild/generate.py`, top of file)

| Variable | Current value |
|---|---|
| `MODEL` | `deepseek/deepseek-v4-flash` — swap for any OpenRouter model |
| `API_URL` | `https://openrouter.ai/api/v1/chat/completions` |
| `TEMPLATE` | `index.template.html` (or `index.html` fallback) |
| `OUTPUT` | `index.html` |
| `TIMEOUT` | 240 s per call |
| Retry | 3 attempts, 6 s × attempt backoff |

## The 24 Rubrics

Defined as `RUBRIKEN` dict in `rebuild/generate.py` (keys `"01"`–`"24"`). Changing a rubric's topic means editing its value in that dict. The HTML template's `data-rubrik` attributes must stay in sync with the keys.

## Automation

`.github/workflows/daily-update.yml` runs at **04:10 UTC** (≈ 05:10/06:10 German time) and can also be triggered manually via `workflow_dispatch`. Each run:

1. Runs `rebuild/generate.py`
2. Uploads `index.html` as a **workflow artifact** (retained 90 days)
3. Commits and pushes `index.html` (only if content changed)
4. Creates or updates a **GitHub Release** — tag `tagesausgabe-YYYY-MM-DD`, name `Tagesausgabe DD.MM.YYYY` — with `index.html` attached as a release asset

Required GitHub secret: `OPENROUTER_API_KEY`. The workflow already has `contents: write`. See `SETUP-github-actions.md` for full deployment steps (German).

## Formatting

- **HTML / JSON / CSS / JS**: Biome (`biome.json`, 2-space indent, LF) — `biome format --write <file>`
- **Python**: black (4-space, line length 100) — `black rebuild/generate.py`
- Editor config via `.editorconfig` (picked up natively by VS Code, JetBrains, etc.)
