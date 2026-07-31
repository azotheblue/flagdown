# Flagdown

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![version](https://img.shields.io/badge/version-0.1.0-green.svg)](pyproject.toml)
[![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/azotheblue/flagdown/pulls)
[![GitHub stars](https://img.shields.io/github/stars/azotheblue/flagdown?style=social)](https://github.com/azotheblue/flagdown)

<p align="center">
  <img src="docs/flagdown-hero.png" alt="Flagdown — Markdown challenges to CTFd" width="800">
</p>

**Flagdown** is an open-source CLI that syncs **markdown** challenges to [CTFd](https://ctfd.io) — built for instructional / course-style CTFs, not weekend jeopardy authoring in `challenge.yml`.

It started life as the upload tooling for a training CTF with **300+ challenges**. After that event, the goal was to rip out the course-specific bits and turn the same workflow into something reusable: write challenges in markdown, declare categories in YAML, sync to CTFd.

```bash
pip install -e .
flagdown --help
```

> [!WARNING]
> This project was built with **heavy agentic engineering** (AI coding agents in the loop). It worked for a real 300+ challenge course CTF, but treat it like early open source: there may be bugs, sharp edges, or security flaws. Review the code before pointing it at production CTFd instances or tokens with broad admin access. PRs and careful eyes welcome.

## Install

```bash
git clone https://github.com/azotheblue/flagdown.git
cd flagdown
pip install -e .
cp .env.example .env
# Set CTFD_URL and CTFD_TOKEN

cp categories.example.yaml categories.yaml
# Edit to match your content folders
```

## CLI

```bash
flagdown list --path /path/to/content
flagdown dry-run --path /path/to/content
flagdown sync --path /path/to/content --all
flagdown sync --path /path/to/content --category linux --update
flagdown sync --path /path/to/content --all --add-requirements
flagdown sync --path /path/to/content --all --fresh --yes

flagdown status
flagdown hide --yes
flagdown delete --category linux --yes

flagdown fix-hints --dry-run
flagdown ui track-nav
flagdown ui sort-by-name
```

| Command | Purpose |
|---------|---------|
| `list` | Discover challenge files |
| `dry-run` | Parse and preview |
| `sync` | Upload / update to CTFd |
| `status` | Live counts from CTFd |
| `hide` / `delete` | Bulk visibility / wipe |
| `fix-hints` | Sequential hint unlock + costs on live instance |
| `ui …` | Paste-ready Theme / Settings snippets |

## Why not ctfcli / Terraform?

| | ctfcli / Terraform | Flagdown |
|---|---|---|
| Source of truth | `challenge.yml` / HCL | Markdown |
| Authoring feel | Spec / infra | Course content |
| Intro gating | Manual | `--add-requirements` |
| Hint cost sequencing | Manual | Built in |

## Category map (`categories.yaml`)

```yaml
categories:
  01-intro:
    type: standard
  02-web:
    name: Web
    type: standard
  03-forensics:
    type: standard
    max_attempts: 3
  05-quiz:
    type: multiple_choice   # needs paid CTFd MC plugin
  06-writeups:
    type: manual            # needs paid CTFd Manual Verification plugin
```

| Field | Meaning |
|-------|---------|
| *(key)* | Folder under `--path` |
| `name` | CTFd category string (default = folder key) |
| `type` | `standard` (stock CTFd), `multiple_choice`, or `manual` |
| `max_attempts` | Default attempt limit for non-intro challenges |
| `layout` | `auto` (default), `flat`, or `nested` — usually leave alone |

See `categories.example.yaml` for a complete starter map.

## Markdown formats (core CTFd)

### Challenge format

```markdown
# Challenge 01 - Title

## Question
Challenge description...

## Files
- evidence.pcap

## Flag
{flag_here}

## Points
100 (Easy)

## Hints
- Hint 1
- Hint 2

## Solution
Walkthrough for admins / grading...
```

### Simple format

```markdown
# Question
What is the answer?

# Flag
answer_here

# Points
20
```

## Optional formats (paid CTFd plugins)

Multiple choice and manual verification require [CTFd's paid challenge plugins](https://ctfd.io/). Flagdown can author them; uploads fail without the plugins.

See `examples/` (plugin examples are marked `.REQUIRES-PLUGIN`).

## Extras

```bash
flagdown fix-hints              # subsequent-hint unlock on live CTFd
flagdown ui track-nav           # sticky category bar → Theme Footer
flagdown ui sort-by-name        # natural name order → core-beta Settings Editor
```

## Contributors

- [azotheblue](https://github.com/azotheblue) — project lead
- [Cursor](https://cursor.com) (CLI / agentic coding) — pair-programmer on the extract, packaging, docs, and tooling

Built with heavy agentic engineering. Humans still own the bugs.

## License

[MIT](LICENSE)
