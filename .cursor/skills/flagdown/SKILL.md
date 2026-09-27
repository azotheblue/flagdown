---
name: flagdown
description: Author Flagdown markdown CTF challenges and sync them to CTFd. Use when writing or editing challenge .md files, categories.yaml, running flagdown list/dry-run/sync/status/hide/delete/fix-hints/ui, or when the user mentions Flagdown, CTFd course challenges, or markdown-to-CTFd upload.
---

# Flagdown

CLI that syncs **markdown** challenges to [CTFd](https://ctfd.io) for course / training CTFs. Human docs: repo `README.md`. Examples: `examples/`.

Do not invent `challenge.yml` / ctfcli layouts. Flagdown's source of truth is markdown + `categories.yaml`.

## Setup

```bash
pip install -e .
cp .env.example .env          # CTFD_URL, CTFD_TOKEN — never commit .env
cp categories.example.yaml categories.yaml
```

`--path` is the content root (folders named in `categories.yaml`). Config is `--config`, else `<path>/categories.yaml`, else `./categories.yaml`.

## Content layout

```
content-root/
  categories.yaml
  01-intro/
    0-intro.md                 # intro / gate (optional)
    questions/01-simple.md
  02-web/
    questions/01-xss.md
    sample.bin                 # attachment next to md or in category folder
```

`categories.yaml` keys are **folder names**. `name` is the CTFd category string (defaults to the key).

| Field | Values |
|---|---|
| `type` | `standard` (stock CTFd), `multiple_choice`, `manual` |
| `max_attempts` | default for non-intro challenges |
| `layout` | `auto` (default), `flat`, `nested` |

`auto`: nested for `standard`, flat for plugin types. Nested finds `questions/*.md`, `*-question.md`, and root `*.md`. Flat finds `*.md` in the folder. Skips `README.md` and `draft*`.

`multiple_choice` / `manual` need paid CTFd plugins. Uploads fail on stock CTFd. Plugin examples are named `*.REQUIRES-PLUGIN.md`.

## Markdown (standard)

**Challenge format** (`##` sections). Title `# Challenge NN - Name` becomes the CTFd name; otherwise the filename stem.

```markdown
# Challenge 01 - Title

## Question
Student-facing description.

## Files
- evidence.pcap

## Flag
flag_here

## Points
100 (Easy)

## Hints
- Hint 1
- Hint 2

## Solution
Admin walkthrough (hidden in CTFd).
```

**Simple format** (`#` sections): `# Question`, `# Flag`, `# Points`, optional `# Hints` / `# Solution`.

Optional sections: `Answer` (alias of Flag), `Answer Type` / `Flag Type` with `regex`, `Max Attempts`. Intro files may use `## Scenario` / `## Overview` instead of `## Question`.

Intro files match names like `0-intro`, `00-intro`. Intros default to 0 points. With `--add-requirements`, non-intro challenges in that category require the intro.

Attachments: list filenames under Files. Resolved next to the markdown, in `questions/`, or the category folder. The same filename used by multiple questions in a category is uploaded on the **intro only**.

New challenges are created **hidden**. `--update` does not change visibility.

Hint costs and sequential unlock are computed on upload (later hints cost more; multiples of 5). Re-apply on a live instance with `flagdown fix-hints`.

## Plugin formats

**Multiple choice** (`type: multiple_choice`): one markdown table, columns `Name | Description | Points | Answers | Correct Answer | How`. Answers use `* () option` (separate with `<br>` or newlines). `How` becomes a hint. MC gets `max_attempts=3`.

**Manual** (`type: manual`): Question / Points / Hints / `Verification Criteria` (admin solution). No flag.

## CLI workflow

Always discover and parse before a live upload:

```bash
flagdown list --path <content>
flagdown dry-run --path <content>
flagdown dry-run --path <content> --category web
```

`sync` requires `--all`, `--category`, or `--file`:

```bash
flagdown sync --path <content> --file 02-web/questions/01-xss.md
flagdown sync --path <content> --category linux --update
flagdown sync --path <content> --all --add-requirements
```

| Command | Use |
|---|---|
| `list` | Discovery only |
| `dry-run` | Parse preview (no API) |
| `sync` | Upload. Default skips existing names |
| `sync --update` | Patch fields, flags, hints, files, solutions |
| `sync --fresh --yes` | Delete matching live challenges, then upload |
| `sync --add-requirements` | Gate on each category's intro |
| `status` | Live counts |
| `hide --yes` | Set hidden |
| `delete --category X --yes` | Destructive wipe |
| `fix-hints --dry-run` | Preview hint cost / unlock repair |
| `ui track-nav` / `ui sort-by-name` | Print snippets to paste into CTFd theme/settings |

`--category` is a **partial** match on the CTFd category name.

## Live-instance rules

- Prefer `dry-run`, then `--file` or `--category`, then `--all`.
- Prefer `--update` over `--fresh`. `--fresh` and `delete` are destructive; require `--yes` only when the user clearly wants a wipe.
- Do not run `--fresh` / `delete` against production without an explicit ask.
- Never print, commit, or log `CTFD_TOKEN` or `.env`.
- `--probe-fallback` is a last resort (slow; floods CTFd logs) when the admin list endpoint returns 500, often from challenge requirements.
