# claude-gateway-extensions — agent instructions

## Scope

The shipped scope is whatever `docs/DESIGN.md` "Scope of 1.0" says it is;
everything under "Roadmap" is designed into that file before code is written.

## Invariants

- Byte-transparent forwarding: request/response headers (minus hop-by-hop)
  and bodies pass through unmodified except for the deliberate bump replay.
- Never log request or response bodies, at any log level.
- `BumpResult`'s cache-write check is not optional: every bump path inspects
  `cache_creation_input_tokens` via the shared classifier, not a status code.
- The daemon listens on `127.0.0.1` by default; a non-local `CGX_HOST` is
  the user's explicit choice and logs a warning, it is never refused.
- `configure`/`unconfigure` write only `env.ANTHROPIC_BASE_URL`,
  `env.CGX_URL` (non-default port only), `hooks.UserPromptSubmit`, one
  `hooks.<event>` per `--shadow-fork NAME[:PREFIX]` (event and matcher from
  the template), and the skill and template copies. `managed` is not a scope.
- `configure`, `unconfigure` and `service install`/`uninstall` show a diff of
  every file they would write and apply it only on consent; `--yes` is the
  sole bypass, and a non-TTY stdin without it writes nothing.
- One module parses a `/cache` argument, `cli/when.py`; the hook, `cgx on
  --until` and any new caller go through `parse_when`.
- Single runtime dependency: `aiohttp`, plus `tzdata` on Windows (`zoneinfo`
  needs it there). Do not add a second HTTP or web framework; the CLI
  client uses `urllib` deliberately, to stay import-light.
- CLI is `argparse`, not `typer`; the root Scripts Typer rule is not ours.

## Tests

- Tests exercise a fake in-process upstream (`tests/conftest.py`) only.
  Never call the real Anthropic API from a test.
- Any test that redirects `HOME`, `XDG_STATE_HOME`, or another path env var
  must assert the redirect actually took effect before relying on it.
- `@pytest.mark.invariant(decision="...")` marks a test guarding a decision
  in `docs/DESIGN.md`; a failure means the code contradicts that decision, so
  fix the code. Weakening one needs an amendment and the user's consent.

## Docs

- README.md's Configuration tables are the only variable reference; every
  `CGX_*` variable in `config.py` appears there with its real default.
- `docs/DESIGN.md` records decisions and their rationale only. Do not add
  module maps, flows or tables that restate the code or the README.
- No emojis anywhere in docs or code comments.

## Agent behavior rules

- Pushing to any git remote requires the user's explicit consent per
  invocation; a mandate such as "publish" or "release" authorises only the
  push it names. Agents commit freely and ask before every push.
- Verification is `make check`, run once before a commit; never chain
  `ruff`/`pyright`/`pytest` by hand. Use `make fix` for formatting.
- `dev` takes day-to-day work; `main` only takes release merges (tags
  `vX.Y.Z`), so it always points at released, working code.
- Conventional commits: `type(scope): topic: specifics`, subject at most 72
  characters, body bullets wrapped at 72.
