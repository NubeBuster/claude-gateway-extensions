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
- The daemon binds loopback only; a non-loopback `CCBG_HOST` is refused
  unless `CCBG_ALLOW_NON_LOOPBACK=true`.
- `ccbg install`/`uninstall` write only `env.ANTHROPIC_BASE_URL` and
  `hooks.UserPromptSubmit` in `settings.json`, plus the skill copy. Never
  add another settings key without updating `docs/DESIGN.md` and both
  README/DESIGN config tables.
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

- README.md's Configuration table and DESIGN.md's Configuration table must
  list the same `CCBG_*` variables with the same defaults; update both
  together.
- No emojis anywhere in docs or code comments.

## Commits

Conventional commits: `type(scope): topic: specifics`, subject at most 72
characters, body bullets wrapped at 72.
