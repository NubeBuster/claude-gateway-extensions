# claude-cache-bump-gateway

`ccbg` is a local transparent HTTP proxy between Claude Code and the
Anthropic API. It records the last `/v1/messages` request of a Claude Code
session and, while bumping is turned on for that session, replays it with
`max_tokens: 0` shortly before the server-side prompt cache would expire. A
cache read refreshes the TTL at no charge, so you can walk away for hours and
come back to a warm cache instead of paying to rebuild it.

For anyone running Claude Code on a claude.ai subscription or an API key who
leaves sessions idle between turns and wants the cache to survive. Cost:
normal cache-read pricing per bump (often free on a subscription); it stops
bumping a session the moment a bump would pay a full cache write instead of
a read (see "How it works").

0.1 ships cache bumping only; other features are on the roadmap in
`docs/DESIGN.md`.

## Requirements

- Python 3.11+
- Claude Code, with a claude.ai subscription or an `ANTHROPIC_API_KEY`
- Linux or macOS. `ccbg install --service` (the systemd user unit) is Linux
  only; on macOS run `ccbg serve` yourself (a shell, `tmux`, or a `launchd`
  agent you write).

## Install

```sh
pipx install claude-cache-bump-gateway
# or: uv tool install claude-cache-bump-gateway
# or, until this is published: pip install git+https://github.com/NubeBuster/claude-cache-bump-gateway
```

Then:

```sh
ccbg install --scope user --service
ccbg doctor
```

`ccbg install` idempotently merges `env.ANTHROPIC_BASE_URL` and a
`UserPromptSubmit` hook into the chosen `settings.json`, and copies the
`cache-bump` skill into the matching `skills/` directory — nothing else in
your `settings.json` is touched. `--service` also writes and enables a
systemd user unit running `ccbg serve`. `ccbg doctor` reports what is and
is not wired up. Use `--scope project` to wire a single project's
`.claude/settings.json` instead of (or alongside) your user one.

Then, inside a Claude Code session: `/cache-bump on`.

## Usage

The `/cache-bump` command inside Claude Code:

| Command              | Effect                                                  |
| -------------------- | ------------------------------------------------------- |
| `/cache-bump on`     | turn bumping on for the current session                 |
| `/cache-bump on 2h`  | turn bumping on with a 2-hour max duration override     |
| `/cache-bump off`    | turn bumping off                                        |
| `/cache-bump status` | show whether bumping is on and when the next bump fires |
| `/cache-bump now`    | bump immediately, outside the normal schedule           |

`on <duration>` accepts `<N>h` or `<N>m` and overrides `max_duration_s` for
that session, not the bump interval.

CLI equivalents (`--session` defaults to `$CLAUDE_SESSION_ID`, which Claude
Code sets in every hook and command it runs):

```sh
ccbg on --session "$CLAUDE_SESSION_ID"
ccbg on --session "$CLAUDE_SESSION_ID" --max-duration 7200
ccbg off --session "$CLAUDE_SESSION_ID"
ccbg status                                        # every known session
ccbg status --session "$CLAUDE_SESSION_ID" --json
ccbg now --session "$CLAUDE_SESSION_ID"
```

To show cache-bump status in your own statusline, append `ccbg statusline`
to your existing `statusLine` command, e.g. `existing-command; ccbg
statusline`. It reads the same statusline JSON from stdin and prints one
short segment (`cache-bump: on, next in 47m`, `cache-bump: off`, or nothing
when the daemon is unreachable or the session is unknown); it never blocks.

## How it works

1. `ANTHROPIC_BASE_URL` points Claude Code at the proxy; no credential is set
   for the proxy itself, so Claude Code's own claude.ai login (or API key)
   is forwarded unchanged.
2. Every request is forwarded byte-for-byte (headers minus hop-by-hop ones,
   body untouched), and responses stream through with no buffering.
3. `POST /v1/messages` requests are additionally captured: the daemon groups
   a session's requests into streams by `model + system + tools` and keeps
   the latest request body and reported `usage` per stream.
4. While bumping is on, a timer fires shortly before the cache TTL would
   lapse (50 min for a 1-hour-TTL prompt, 4 min otherwise) and replays the
   largest captured stream's last request with `max_tokens: 0` and
   `stream: false`. The response is discarded; only `usage` is inspected.
5. `cache_creation_input_tokens == 0` means the read hit the cache and the
   timer resets. Otherwise the cache was already gone and the bump paid for
   a full write — by default the daemon stops bumping that session.

Full design and rationale, including the session/stream model, stop
conditions and every edge case: `docs/DESIGN.md`.

## Configuration

Everything is environment variables, prefix `CCBG_`. Set them in the `env`
block of the Claude Code `settings.json` scope that should apply (user or
project), or in the daemon's environment directly — the systemd unit passes
through every `CCBG_*` variable present when you ran `ccbg install --service`.

| Variable                       | Default                                                       | Meaning                                                                                 |
| ------------------------------ | ------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `CCBG_HOST`                    | `127.0.0.1`                                                   | listen address; anything but loopback is refused without `CCBG_ALLOW_NON_LOOPBACK=true` |
| `CCBG_PORT`                    | `8787`                                                        | listen port                                                                             |
| `CCBG_UPSTREAM`                | `https://api.anthropic.com`                                   | forward target                                                                          |
| `CCBG_STATE_DIR`               | `${XDG_STATE_HOME:-~/.local/state}/claude-cache-bump-gateway` | persistence root                                                                        |
| `CCBG_PERSIST`                 | `true`                                                        | write snapshots and state to disk                                                       |
| `CCBG_DEFAULT_ENABLED`         | `false`                                                       | bump new sessions without being asked                                                   |
| `CCBG_INTERVAL_1H_S`           | `3000`                                                        | interval when the prompt requests 1 h TTL                                               |
| `CCBG_INTERVAL_5M_S`           | `240`                                                         | interval otherwise                                                                      |
| `CCBG_MAX_DURATION_S`          | `86400`                                                       | stop bumping this long after the last real request                                      |
| `CCBG_ON_CACHE_WRITE`          | `stop`                                                        | `stop` or `continue` when a bump paid a cache write                                     |
| `CCBG_MAX_STREAMS_PER_SESSION` | `8`                                                           | snapshots retained per session                                                          |
| `CCBG_LOG_LEVEL`               | `INFO`                                                        | Python logging level                                                                    |
| `CCBG_URL`                     | `http://127.0.0.1:8787`                                       | where CLI subcommands find the daemon                                                   |
| `CCBG_ALLOW_NON_LOOPBACK`      | `false`                                                       | allow `CCBG_HOST` to bind a non-loopback address                                        |

This table is kept in sync with `docs/DESIGN.md` "Configuration"; if they
ever disagree, `config.py` is the source of truth.

## Integrating

The control API (`/ccbg/*` on the same listener, JSON, loopback-only,
unauthenticated) is the integration point for anything that is not this
project — PTY wrappers, editor plugins, other statuslines. Full contract:
`docs/API.md`.

```sh
curl -s -X PUT "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID" \
  -H 'content-type: application/json' -d '{"enabled": true}'
curl -s "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID"
curl -s -X POST "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID/bump"
```

## Security and privacy

- The control API is loopback-only and unauthenticated; it can spend your
  subscription quota, so never expose it beyond `127.0.0.1` or put it behind
  a port forward.
- Persisted snapshots hold the full conversation and the request credential
  (they are what gets replayed), at file mode 0600 inside a 0700 directory.
  Set `CCBG_PERSIST=false` to keep everything in memory only, at the cost of
  losing bump state across daemon restarts.
- The proxy never logs request or response bodies. `CCBG_LOG_LEVEL=DEBUG`
  logs headers with credential values redacted.

## Notes

- `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1` is not set by the installer.
  Its only known effect is letting Claude Code verify native 1M-context
  models behind a custom base URL. It is undocumented and
  underscore-prefixed upstream; set it yourself if you need that, aware it
  may change or break without notice.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or an `apiKeyHelper` all
  replace the forwarded subscription credential and change what gets billed;
  `ccbg doctor` flags any of them being set.
- To uninstall: `ccbg uninstall --scope user --service` (match whatever
  scope and `--service` flag you installed with). Reverses exactly the
  settings.json edits `ccbg install` made and removes the systemd unit.
  It also deletes the installed skill directory outright, including any
  local edits you made to its `SKILL.md`.

## Current planned `TODOs`

- Choose a better broader name for this project sooner than later, move it, update links and references.

- Add testing setup for preventing regressions during scope expansion and development
- Add `shadow fork` capability including
  - `/name` customizability giving full control over the `/rename` without extra overhead.
  - custom subagent configuration capabilities adhering where possible to the offiical [Claude Code frontmatter fields format](https://code.claude.com/docs/en/sub-agents#supported-frontmatter-fields) where sensible
  - a ready to use implementation for a specific pattern this allows for: `fork-review` hook self assessment.
    For example on TaskCreate at _PreToolUse_ the agent reviews it's own tool call parameters, for example the teammate picked to assign to a Task, it can give some verdicts:
    - `OK` - the session proceeds and the main session is unaffected entirely.
    - `reject, +reason injected into additionalContext` - **only** if an intervention is decided for does this provide compounding overhead (context growth, disruption)
    - `mutate tooluse input arguments` & `optional info injected into additionalContext` - auto correct itself with it's own model, context, and an entire one-shot prompt that hbas no persistent overhead. **This can replace many rejecting guardrail hooks with steering and auto correction by a classifier step**
    - `run some script conditioned on the agent's own decisicion` - the decision is forgotten and the main session/agenmt is none the wiser'
      This allows for [agent-based-hooks](https://code.claude.com/docs/en/hooks#agent-based-hooks) but with the parent context, parent cache hit, and thus low overhead single-shot micro-fork type of agent based hook implementations. Making agent based hooks possibly viable, in contrast to the native implementation.
- Implement support for Windows

## Development

```sh
git clone https://github.com/NubeBuster/claude-cache-bump-gateway
cd claude-cache-bump-gateway
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
# or: uv sync --extra dev
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pyright
.venv/bin/pytest
```

MIT licensed, see `LICENSE`.
