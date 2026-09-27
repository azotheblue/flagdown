# Changelog

## 0.2.0

- Nested `examples/` mini course (`flagdown list --path examples`)
- Agent skill at `.cursor/skills/flagdown/`
- `sync --file` resolves a single discovered markdown file
- `--update` syncs flags, hints, files, and solutions (not only challenge fields)
- `status` / `hide` hydrate challenge details (CTFd 3.8 often omits `state` on the list)
- Clearer errors when paid challenge plugins are missing
- `--fresh` scoped to the `--file` challenge when used together

## 0.1.0

Initial public extract: markdown-to-CTFd CLI.
