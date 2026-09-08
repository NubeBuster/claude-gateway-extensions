# claude-cache-bump-gateway — agent instructions

## Scope

The shipped scope is whatever `docs/DESIGN.md` "Scope of 0.1" says it is;
today that is one thing: keep a Claude Code session's prompt cache warm by
replaying its last request with `max_tokens: 0` before the TTL expires. Later
features are listed as candidates under DESIGN.md "Roadmap". Do not implement
one here until it has been designed into `docs/DESIGN.md` first; a roadmap
entry is not an approval to write the code.

## Invariants

- Byte-transparent forwarding: request/response headers (minus hop-by-hop)
  and bodies pass through unmodified except for the deliberate bump replay.
- Never log request or response bodies, at any log level.
- `BumpResult`'s cache-write check is not optional — every bump path
  inspects `cache_creation_input_tokens` via the shared classifier, never a
  bare status-code check.
- The daemon listens on `127.0.0.1` by default; a non-local `CCBG_HOST` is
  the user's explicit choice and logs a warning, it is never refused.
- `ccbg install`/`uninstall` write only `env.ANTHROPIC_BASE_URL` and
  `hooks.UserPromptSubmit` in `settings.json`, plus the skill copy. Never
  add another settings key without recording the decision in
  `docs/DESIGN.md` and updating the README Configuration tables.
- Single runtime dependency: `aiohttp`. Do not add a second HTTP or web
  framework; the CLI client uses `urllib` deliberately, to stay import-light.
- CLI is `argparse`, not `typer` (overrides the root Scripts CLAUDE.md's
  Typer preference — that rule is for `~/Scripts`, not this repo).

## Tests

- Tests exercise a fake in-process upstream (`tests/conftest.py`) only.
  Never call the real Anthropic API from a test.
- Any test that redirects `HOME`, `XDG_STATE_HOME`, or another path env var
  must assert the redirect actually took effect before relying on it.

## Docs

- README.md's Configuration tables are the only variable reference; every
  `CCBG_*` variable in `config.py` appears there with its real default.
- `docs/DESIGN.md` records decisions and their rationale only. Do not add
  module maps, flows or tables that restate the code or the README.
- No emojis anywhere in docs or code comments.

## Agent behavior rules

- Pushing to any git remote requires the user's explicit consent, given per
  invocation. A general mandate such as "publish", "release" or "deploy"
  authorises only the push it names, never a later one. Agents commit
  freely and ask before every push.
- Verification is `make check`, run once before a commit; never chain
  `ruff`/`pyright`/`pytest` by hand. Use `make fix` for formatting.

## Commits

Conventional commits: `type(scope): topic: specifics`, subject at most 72
characters, body bullets wrapped at 72.
