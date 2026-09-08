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
`cache` skill into the matching `skills/` directory — nothing else in
your `settings.json` is touched. `--service` also writes and enables a
systemd user unit running `ccbg serve`. `ccbg doctor` reports what is and
is not wired up. Use `--scope project` to wire a single project's
`.claude/settings.json` instead of (or alongside) your user one.

Then, inside a Claude Code session: `/cache on`.

## Usage

The `/cache` command inside Claude Code:

| Command         | Effect                                                  |
| --------------- | -------------------------------------------------------- |
| `/cache`        | same as `/cache status`                                 |
| `/cache on`     | turn bumping on for the current session                 |
| `/cache on 2h`  | turn bumping on with a 2-hour max duration override     |
| `/cache off`    | turn bumping off                                        |
| `/cache status` | show whether bumping is on and when the next bump fires |
| `/cache bump`   | bump immediately, outside the normal schedule           |

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

`ccbg status --session "$CLAUDE_SESSION_ID" --json` and `GET
/ccbg/sessions/{id}` both return the same session status object:

```json
{
  "session_id": "abc123",
  "enabled": true,
  "interval_s": null,
  "max_duration_s": null,
  "last_request_at": 1700000000.0,
  "last_bump_at": 1700003000.0,
  "bump_count": 3,
  "stopped_reason": null,
  "next_bump_at": 1700006000.0,
  "streams": 2,
  "last_result": {
    "outcome": "ok",
    "status": 200,
    "usage": {
      "input_tokens": 10,
      "output_tokens": 0,
      "cache_read_input_tokens": 4096,
      "cache_creation_input_tokens": 0
    },
    "detail": "",
    "stream_key": "ab12...",
    "model": "claude-opus-4"
  }
}
```

| Field             | Type                       | Meaning                                                              |
| ------------------ | -------------------------- | ---------------------------------------------------------------------- |
| `session_id`       | string                     | the session this status is for                                       |
| `enabled`          | bool                       | whether bumping is currently on                                      |
| `interval_s`       | int or null                | per-session bump interval override; `null` uses the config default   |
| `max_duration_s`   | int or null                | per-session max-duration override; `null` uses the config default    |
| `last_request_at`  | float (epoch s) or null    | when the last real `/v1/messages` request was captured               |
| `last_bump_at`     | float (epoch s) or null    | when the last successful bump replay ran                             |
| `bump_count`       | int                        | number of successful bumps for this session                          |
| `stopped_reason`   | string or null             | why bumping was turned off automatically (e.g. `cache_write`), if it was |
| `next_bump_at`     | float (epoch s) or null    | when the next scheduled bump will fire; `null` if disabled or no snapshot |
| `streams`          | int                        | number of distinct request streams captured for this session         |
| `last_result`      | object or null             | the most recent `BumpResult` (below), or `null` if never bumped       |
| `last_result.outcome`      | string             | `ok`, `cache_write`, `unauthorized`, `rejected`, or `transient`       |
| `last_result.status`       | int or null        | upstream HTTP status of the bump replay                              |
| `last_result.usage`        | object or null      | token usage from the replay response (fields below), or `null`       |
| `last_result.usage.input_tokens`                | int | fresh (non-cached) input tokens billed                |
| `last_result.usage.output_tokens`               | int | output tokens billed                                   |
| `last_result.usage.cache_read_input_tokens`     | int | input tokens served from cache (a successful bump)     |
| `last_result.usage.cache_creation_input_tokens` | int | input tokens billed as a fresh cache write              |
| `last_result.detail`       | string              | short human-readable detail, empty on `ok`                           |
| `last_result.stream_key`   | string or null      | which captured stream was replayed                                   |
| `last_result.model`        | string or null      | the model of the replayed stream                                     |

Full contract, including every other route: `docs/API.md`.

To show cache status in your own statusline, append `ccbg statusline`
to your existing `statusLine` command, e.g. `existing-command; ccbg
statusline`. It reads the same statusline JSON from stdin and prints one
short segment (`cache: on, next in 47m`, `cache: off`, or nothing
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

The decisions behind this and why they were made: `docs/DESIGN.md`.

## Configuration

You do not need to configure anything. Put this in `~/.claude/settings.json`,
or let `ccbg install` do it:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"
  },
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "ccbg hook user-prompt-submit",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Anything below goes in that same `env` object, next to `ANTHROPIC_BASE_URL`.
Where a variable takes effect depends on who reads it, so it splits into two
tables below.

### Session settings (put these in settings.json)

Claude Code applies these per user, project, or local scope, and the hook
forwards the interval and duration ones to the daemon as this session's
overrides when you run `/cache on` (`CCBG_URL` just tells the hook, the CLI
and the statusline where to find the daemon).

| Variable              | Default                 | What it does                                                             |
| ---------------------- | -------------------------- | ------------------------------------------------------------------------- |
| `CCBG_INTERVAL_1H_S`  | `3000`                   | how often to refresh the cache when the model gives it a longer lifetime |
| `CCBG_INTERVAL_5M_S`  | `240`                    | how often to refresh the cache otherwise                                |
| `CCBG_MAX_DURATION_S` | `86400`                  | stop refreshing this long after you last sent a real message            |
| `CCBG_URL`            | `http://127.0.0.1:8787`  | where the hook, the CLI and the statusline find the daemon              |

### Daemon settings (set where the daemon runs)

The daemon does not read `settings.json`. `ccbg install --service` copies
any of these that are set in your shell at install time into the systemd
unit file, and otherwise you export them yourself before running `ccbg
serve`. To change one later without reinstalling, run `systemctl --user
edit claude-cache-bump-gateway.service`.

| Variable                       | Default                                                       | What it does                                                     |
| ------------------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------- |
| `CCBG_HOST`                    | `127.0.0.1`                                                   | local only (`127.0.0.1`) by default; who can reach the daemon    |
| `CCBG_PORT`                    | `8787`                                                        | which port the daemon listens on                                 |
| `CCBG_UPSTREAM`                | `https://api.anthropic.com`                                   | where requests are actually forwarded to                         |
| `CCBG_STATE_DIR`               | `${XDG_STATE_HOME:-~/.local/state}/claude-cache-bump-gateway` | where session state and captured requests are saved               |
| `CCBG_PERSIST`                 | `true`                                                        | save state to disk so it survives a daemon restart                |
| `CCBG_DEFAULT_ENABLED`         | `false`                                                       | turn bumping on automatically for every new session               |
| `CCBG_ON_CACHE_WRITE`          | `stop`                                                        | `stop` bumping or `continue` when a refresh finds the cache already expired |
| `CCBG_MAX_STREAMS_PER_SESSION` | `8`                                                           | how many separate conversation branches to remember per session   |
| `CCBG_LOG_LEVEL`               | `INFO`                                                        | how much detail the daemon logs                                   |

If a table and the code ever disagree, `config.py` is the source of truth.

## Integrating

The control API (`/ccbg/*` on the same listener, JSON, local-only by
default, unauthenticated) is the integration point for anything that is not this
project — PTY wrappers, editor plugins, other statuslines. Full contract:
`docs/API.md`.

```sh
curl -s -X PUT "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID" \
  -H 'content-type: application/json' -d '{"enabled": true}'
curl -s "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID"
curl -s -X POST "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID/bump"
```

## Security and privacy

- The control API is unauthenticated and listens on `127.0.0.1` by
  default; it can spend your subscription quota, so never expose it beyond
  `127.0.0.1` or put it behind a port forward.
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

The `cache` skill lives at `claude/skills/cache/SKILL.md` in the repo root;
`ccbg install` copies it into the target `skills/` directory. Cloned instead
of installed and want it without running `ccbg install`? Copy
`claude/skills/cache/` into `~/.claude/skills/` (or a project's
`.claude/skills/`) yourself.

MIT licensed, see `LICENSE`.
