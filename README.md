# claude-gateway-extensions

`cgx` is a local transparent HTTP proxy between Claude Code and the
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
- Claude Code
- [Both claude.ai subscriptions and API billing are supported](https://code.claude.com/docs/en/gateways#subscriptions-and-gateways)
- Linux or macOS. `cgx service install` (the systemd user unit) is Linux
  only; on macOS run `cgx serve` yourself (a shell, `tmux`, or a `launchd`
  agent you write). Running without systemd: `cgx serve --port 8787` in any
  process manager or terminal, then `cgx configure --port 8787`. To upgrade
  without dropping a connection, start the new binary with `cgx serve --port
8787 --handover`: it binds alongside the running daemon and asks it to
  drain and exit once the new one is healthy (Linux/macOS only, needs
  `SO_REUSEPORT`; under systemd, `cgx service restart` already drains in
  place).

## Installation

```sh
pipx install claude-gateway-extensions
# or: uv tool install claude-gateway-extensions
# or, until this is published: pip install git+https://github.com/NubeBuster/claude-gateway-extensions
```

### Automatic Installers

For manual installation see [the sections below](#manual-configuration)

**Windows and macOS**: the installer above (`cgx service install`) manages a
systemd user unit and is Linux only. On Windows or macOS, run the daemon
yourself under any process manager — a terminal, `tmux`, Task Scheduler, a
`launchd` agent — and point Claude Code at it manually:

```sh
cgx serve --port 8787
cgx configure --scope user --port 8787
```

Snapshot files still get 0700/0600 permissions on Linux and macOS; on
Windows they instead inherit the user profile's ACLs, since `chmod` has no
equivalent there.

To install the gateway proxy daemon service:

```sh
cgx service install
```

The service install requires no root, but reserves a port system wide. For concurrent proxies on the same machine, separate ports must be assigned.

Both `cgx service install` and `cgx configure` accept `--port N` to pick a
non-default port; keep the two in sync (see "Running without systemd"
above).

---

Then configure Claude Code for routing to the gateway, installing hooks and adding `/cache` skill:

```sh
cgx configure --scope user
# Or skip diff confirmation
cgx configure --scope user -y
```

> [!CAUTION]
> This command affects all running Claude Code sessions in the given scope immediately if you confirm. If any issues occur, this could cause cache invalidations, for example. Don't have 5 Fable sessions with 500k warm cache open. Not worth the risk.

This:

- writes to the scope's (`managed-`)`settings.json`:
  - The [Deamon Environment Variables](#daemon-environment-variables)
  - configures [The Claude Code Hook](#claude-code-hook)
- copies the [cache/SKILL.md](claude/skills/cache/SKILL.md) (fake, for autocomplete, optional) into the scope `.claude/hooks/` - pending confirmation with a diff if a conflicting file exists

All of these writes are gated by your confirmation in the CLI, showing a colored diff for writes and conflicts that would be overridden on confirm.

---

```sh
cgx doctor
```

`cgx doctor` reports what is and
is not wired up. Use `--scope project` to wire a single project's
`.claude/settings.json` instead of (or alongside) your user one.

Then, inside a Claude Code session: `/cache 2h`.

## Usage

The `/cache` command inside Claude Code is installed as a SKILL.md to provide autocomplete, but is intercepted by the `UserPromptSubmit` hook and the agent never sees it.

| Command                   | Effect                                                                            |
| ------------------------- | --------------------------------------------------------------------------------- |
| `/cache`                  | same as `/cache status`                                                           |
|                           |                                                                                   |
| `/cache <deadline>`       | turn bumping on until a deadline (e.g. `2h`, `9PM` or even `teatime`)             |
| `/cache tomorrow teatime` | session cache will be kept warm until tomorrow `16:00` sharp, when the pot is hot |
|                           |                                                                                   |
| `/cache off`              | turn bumping off (also `/cache 0`)                                                |
| `/cache status`           | show whether bumping is on and when the next bump fires                           |
| `/cache bump`             | bump immediately, outside the normal schedule                                     |

Bumping is always until a deadline to prevent bankruptcy by forgetfulness.
The deadline you provide is validated and you are given back the resolved time in local ISO for clarity.

| Format            | Examples                                                        | Meaning                                                                   |
| ----------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Relative duration | `2h`, `90m`, `2d 4h`                                            | duration from now (joined `2d4h` also accepted)                           |
| Clock time        | `9PM`, `21:00`, `04:20`                                         | today at that time, or tomorrow if already past                           |
| Ambiguous time    | `4:20` - is that 4:20PM or AM?                                  | the next 4:20, morning or afternoon; write `04:20`/`16:20` to be explicit |
| Named time        | `morning`, `noon`, `teatime`, `dinner`, `nighttime`, `midnight` | `09:00`, `12:00`, `16:00`, `18:00`, `22:00`, `00:00`                      |
| Relative day      | `tomorrow 10AM`, `today 6PM`                                    | at that time (`tomorrow` alone = 24 hours from now)                       |
| Weekday           | `wed noon`, `fri 9AM`, `mo`, `tues`, `sunday`                   | the weekday's next occurrence, at that time if given                      |
| Absolute date     | `2026-12-31 08:00`                                              | local ISO 8601 date and time                                              |

A deadline already in the past, or an unsupported date format such as `4/20/2027`,
is rejected with an error message.

CLI equivalents (`--session` defaults to `$CLAUDE_SESSION_ID`, which Claude
Code sets in every hook and command it runs):

```sh
cgx on --session "$CLAUDE_SESSION_ID" --until 2h
cgx on --session "$CLAUDE_SESSION_ID"             # daemon's default max duration
cgx off --session "$CLAUDE_SESSION_ID"
cgx status                                        # every known session
cgx status --session "$CLAUDE_SESSION_ID" --json
cgx now --session "$CLAUDE_SESSION_ID"
```

`cgx status` (without `--json`) also prints a git-log tree view of the
daemon's running commit -- up to three commits before it, the running commit
marked `[RUNNING]` with how far behind its branch it is, and up to five
commits toward the branch tip -- whenever the daemon reports git provenance
and the `cgx` command itself is running from a checkout with the matching
history; a plain install, or either side missing git, silently omits it.
Colours (faint tree lines, a coloured `[RUNNING]` badge, a highlighted
uptime) appear only when stdout is a terminal.

`cgx status --session "$CLAUDE_SESSION_ID" --json` and `GET
/cgx/sessions/{id}` both return the same session status object. It always
carries a short `formatted` block of human-readable strings for display:

```json
{
  "formatted": {
    "ttl_countdown": "55:19",
    "expires_at": "20:18",
    "next_bump_countdown": "50:19",
    "next_bump_at": "20:13"
  }
}
```

`ttl_countdown` and `expires_at` are provided for every session that has a
captured snapshot, whether or not bumping is configured, so you can always
display the prompt cache's remaining time to live; `next_bump_countdown` and
`next_bump_at` are `null` when no bump is scheduled.

<details>
<summary>Full status object and field reference</summary>

```json
{
  "session_id": "abc123",
  "enabled": true,
  "interval_s": null,
  "max_duration_s": null,
  "until": null,
  "last_request_at": 1700000000.0,
  "last_bump_at": 1700003000.0,
  "bump_count": 3,
  "stopped_reason": null,
  "next_bump_at": 1700006000.0,
  "cache_expires_at": 1700003300.0,
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
  },
  "formatted": {
    "ttl_countdown": "55:19",
    "expires_at": "20:18",
    "next_bump_countdown": "50:19",
    "next_bump_at": "20:13"
  }
}
```

| Field                                           | Type                    | Meaning                                                                                       |
| ----------------------------------------------- | ----------------------- | --------------------------------------------------------------------------------------------- |
| `session_id`                                    | string                  | the session this status is for                                                                |
| `enabled`                                       | bool                    | whether bumping is currently on                                                               |
| `interval_s`                                    | int or null             | per-session bump interval override; `null` uses the config default                            |
| `max_duration_s`                                | int or null             | per-session max-duration override; `null` uses the config default                             |
| `until`                                         | float (epoch s) or null | absolute deadline that replaces the max-duration stop, if one was set                         |
| `last_request_at`                               | float (epoch s) or null | when the last real `/v1/messages` request was captured                                        |
| `last_bump_at`                                  | float (epoch s) or null | when the last successful bump replay ran                                                      |
| `bump_count`                                    | int                     | number of successful bumps for this session                                                   |
| `stopped_reason`                                | string or null          | why bumping was turned off automatically (e.g. `cache_write`), if it was                      |
| `next_bump_at`                                  | float (epoch s) or null | when the next scheduled bump will fire; `null` if disabled or no snapshot                     |
| `cache_expires_at`                              | float (epoch s) or null | when the prompt cache is expected to expire; `null` if no snapshot yet                        |
| `streams`                                       | int                     | number of distinct request streams captured for this session                                  |
| `last_result`                                   | object or null          | the most recent `BumpResult` (below), or `null` if never bumped                               |
| `last_result.outcome`                           | string                  | `ok`, `cache_write`, `unauthorized`, `rejected`, or `transient`                               |
| `last_result.status`                            | int or null             | upstream HTTP status of the bump replay                                                       |
| `last_result.usage`                             | object or null          | token usage from the replay response (fields below), or `null`                                |
| `last_result.usage.input_tokens`                | int                     | fresh (non-cached) input tokens billed                                                        |
| `last_result.usage.output_tokens`               | int                     | output tokens billed                                                                          |
| `last_result.usage.cache_read_input_tokens`     | int                     | input tokens served from cache (a successful bump)                                            |
| `last_result.usage.cache_creation_input_tokens` | int                     | input tokens billed as a fresh cache write                                                    |
| `last_result.detail`                            | string                  | short human-readable detail, empty on `ok`                                                    |
| `last_result.stream_key`                        | string or null          | which captured stream was replayed                                                            |
| `last_result.model`                             | string or null          | the model of the replayed stream                                                              |
| `formatted.ttl_countdown`                       | string or null          | cache time-to-live countdown, `MM:SS`/`H:MM:SS`, present whenever `cache_expires_at` is known |
| `formatted.expires_at`                          | string or null          | local `HH:MM` clock time the cache expires, present whenever `cache_expires_at` is known      |
| `formatted.next_bump_countdown`                 | string or null          | countdown to the next scheduled bump, `null` if none is scheduled                             |
| `formatted.next_bump_at`                        | string or null          | local `HH:MM` clock time of the next scheduled bump, `null` if none                           |

Full contract, including every other route: `docs/API.md`.

</details>

To show cache status in your own statusline, append `cgx statusline`
to your existing `statusLine` command, e.g. `existing-command; cgx
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

## Manual Configuration

The [automatic installers](#automatic-installers) above can do this automatically. For manual configuration;

### Claude Code Settings

Claude Code applies these per user, project, or local scope (`CGX_URL`
tells the hook, the CLI and the statusline where to find the daemon). The
deadline you type at `/cache <duration|clock|date>` — or pass to `cgx on
--until` — is the actual per-session policy; it is sent straight to the
daemon and does not go through a settings.json variable.

| Variable                | Default                                                                                    | What it does                                                                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CGX_URL`               | `http://127.0.0.1:8787`                                                                    | where the hook, the CLI and the statusline find the daemon; falls back to a loopback `ANTHROPIC_BASE_URL` (which `cgx configure` sets) when unset |
| `CGX_SHADOW_FORK_QUIET` | unset (false)                                                                              | suppress `cgx shadow-fork`'s fail-open `systemMessage` when a review is skipped                                                                   |
| `CGX_TEMPLATE_DIR`      | `${XDG_CONFIG_HOME:-~/.config}/cgx/templates` (POSIX); `%APPDATA%\cgx\templates` (Windows) | where `cgx shadow-fork` looks up a named `.md` definition                                                                                         |

#### Claude Code Hook

You do not need to configure anything. Put this in `~/.claude/settings.json`,
or let `cgx configure` do it:

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
            "command": "cgx hook user-prompt-submit",
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

### Daemon Environment Variables

The daemon does not read `settings.json`. `cgx service install` copies
any of these that are set in your shell at install time into the systemd
unit file, and otherwise you export them yourself before running `cgx
serve`. To change one later without reinstalling, run `systemctl --user
edit claude-gateway-extensions.service`.

| Variable                         | Default                                                                                                                     | What it does                                                                                                                               |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `CGX_HOST`                       | `127.0.0.1`                                                                                                                 | local only (`127.0.0.1`) by default; who can reach the daemon                                                                              |
| `CGX_PORT`                       | `8787`                                                                                                                      | which port the daemon listens on                                                                                                           |
| `CGX_UPSTREAM`                   | `https://api.anthropic.com`                                                                                                 | where requests are actually forwarded to                                                                                                   |
| `CGX_STATE_DIR`                  | `${XDG_STATE_HOME:-~/.local/state}/claude-gateway-extensions` (POSIX); `%LOCALAPPDATA%\claude-gateway-extensions` (Windows) | where session state and captured requests are saved                                                                                        |
| `CGX_PERSIST`                    | `true`                                                                                                                      | save state to disk so it survives a daemon restart                                                                                         |
| `CGX_DEFAULT_ENABLED`            | `false`                                                                                                                     | turn bumping on automatically for every new session                                                                                        |
| `CGX_INTERVAL_1H_S`              | `3000`                                                                                                                      | how often to refresh the cache when the model gives it a longer lifetime                                                                   |
| `CGX_INTERVAL_5M_S`              | `240`                                                                                                                       | how often to refresh the cache otherwise                                                                                                   |
| `CGX_MAX_DURATION_S`             | `86400`                                                                                                                     | stop refreshing this long after the last real message, when no `/cache` deadline was given                                                 |
| `CGX_ON_CACHE_WRITE`             | `stop`                                                                                                                      | `stop` bumping or `continue` when a refresh finds the cache already expired                                                                |
| `CGX_MAX_STREAMS_PER_SESSION`    | `8`                                                                                                                         | how many separate conversation branches to remember per session                                                                            |
| `CGX_LOG_LEVEL`                  | `INFO`                                                                                                                      | how much detail the daemon logs                                                                                                            |
| `CGX_DRAIN_TIMEOUT_S`            | `600`                                                                                                                       | on shutdown, how long to wait for in-flight requests (incl. SSE streams) before exiting                                                    |
| `CGX_SHADOW_FORK_MAX_TOKENS`     | `512`                                                                                                                       | `max_tokens` cap for a review-fork reply, on top of any thinking budget                                                                    |
| `CGX_SHADOW_FORK_MAX_PER_MIN`    | `6`                                                                                                                         | how many shadow forks a single session may fire in a trailing 60s window                                                                   |
| `CGX_SHADOW_FORK_THINKING_GUARD` | `true`                                                                                                                      | refuse a fork against a thinking snapshot on a model below its measured thinking-preservation minimum, instead of paying a full cold write |
| `CGX_SHADOW_FORK_MODEL`          | unset                                                                                                                       | send shadow forks to this model instead of the snapshot's own; forfeits the cached prefix, since Anthropic namespaces the cache per model  |
| `CGX_CONFIG_FILE`                | `${XDG_CONFIG_HOME:-~/.config}/cgx/config.toml` (POSIX); `%APPDATA%\cgx\config.toml` (Windows)                              | the hot-reloaded Shadow Fork tuning file, see below                                                                                        |

Every variable in this table is read once when `cgx serve` starts and
frozen for that run; changing one needs a daemon restart. The hook-side
variables above (`CGX_SHADOW_FORK_QUIET`, `CGX_TEMPLATE_DIR`) have no such
lag: `cgx shadow-fork` reads them fresh on every hook invocation, as it
does the templates themselves.

### Shadow Fork tuning file (hot reload)

The four Shadow Fork settings (`CGX_SHADOW_FORK_MAX_TOKENS`,
`CGX_SHADOW_FORK_MAX_PER_MIN`, `CGX_SHADOW_FORK_THINKING_GUARD`,
`CGX_SHADOW_FORK_MODEL`) can also be set in `CGX_CONFIG_FILE` (default
`~/.config/cgx/config.toml`, next to your templates), where a change
takes effect on the next fork without restarting `cgx serve`:

```toml
[shadow_fork]
max_tokens = 768
max_per_min = 10
thinking_guard = true
# model = "<model id>"  # omitted: fork on the session's own model (cache read)
```

For these four keys the file wins over the environment, the environment
wins over the built-in default, and a fork request's own `max_tokens` or
`model` field wins over all three. A key the file does not mention falls
through to the environment value the daemon started with. Nothing else
can be set in the file; every other daemon variable stays
environment-only.

The daemon re-reads the file when its modification time changes, checked
on each fork request and each `GET /cgx/config`. The file applies as a
whole or not at all: a syntax error, an unknown key or a wrongly typed
value keeps the previously loaded values (or the environment's) and logs
one WARNING naming the error; a missing file means the environment. `GET
/cgx/config` (see `docs/API.md`) shows the resolved path, whether it
loaded, and every effective value with its source (`file`, `env` or
`default`); `cgx doctor` reports the same file, its parse error if any,
and whether the running daemon resolves the same path this shell does.

Hooks themselves are a different matter: Claude Code reads
`settings.json` `hooks` at session start, so a `cgx configure
--shadow-fork` run applies to sessions started after it.

If a table and the code ever disagree, `config.py` is the source of truth.

## Integrating

The control API (`/cgx/*` on the same listener, JSON, local-only by
default, unauthenticated) is the integration point for anything that is not this
project — PTY wrappers, editor plugins, other statuslines. Full contract:
`docs/API.md`.

```sh
curl -s -X PUT "http://127.0.0.1:8787/cgx/sessions/$SESSION_ID" \
  -H 'content-type: application/json' -d '{"enabled": true}'
curl -s "http://127.0.0.1:8787/cgx/sessions/$SESSION_ID"
curl -s -X POST "http://127.0.0.1:8787/cgx/sessions/$SESSION_ID/bump"
```

## Security and privacy

- The control API is unauthenticated and listens on `127.0.0.1` by
  default; it can spend your subscription quota, so never expose it beyond
  `127.0.0.1` or put it behind a port forward.
- Persisted snapshots hold the full conversation and the request credential
  (they are what gets replayed), at file mode 0600 inside a 0700 directory.
  Set `CGX_PERSIST=false` to keep everything in memory only, at the cost of
  losing bump state across daemon restarts.
- The proxy never logs request or response bodies. `CGX_LOG_LEVEL=DEBUG`
  logs headers with credential values redacted.

## Notes

- `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1` is not set by the installer.
  Its only known effect is letting Claude Code verify native 1M-context
  models behind a custom base URL. It is undocumented and
  underscore-prefixed upstream; set it yourself if you need that, aware it
  may change or break without notice.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or an `apiKeyHelper` all
  replace the forwarded subscription credential and change what gets billed;
  `cgx doctor` flags any of them being set.
- To uninstall: `cgx unconfigure --scope user` (match whatever scope you
  configured with) and, if you also ran `cgx service install`,
  `cgx service uninstall`. `cgx unconfigure` reverses exactly the
  settings.json edits `cgx configure` made and deletes the installed skill
  directory outright, including any local edits you made to its
  `SKILL.md`; `cgx service uninstall` disables and removes the systemd
  unit.

## Current planned `TODOs`

- Hot reload: pick up a definition's changes without a `cgx serve` restart
  -- currently frozen at daemon start.
- Ensure support for Windows.
- Prune the test bloat that Opus 5 creates. Add preventative instructions
- Add memory md files maintenance instructions to ensure quality

Shadow Fork reviews a hook event with a one-shot fork of the session's own
cached context, then maps the verdict to that event's hook output:

- `OK` - the session proceeds and the main session is unaffected entirely.
- `reject, +reason injected into additionalContext` - **only** if an intervention is decided for does this provide compounding overhead (context growth, disruption)
- `mutate tooluse input arguments` & `optional info injected into additionalContext` - auto correct itself with its own model, context, and an entire one-shot prompt that has no persistent overhead. **This can replace many rejecting guardrail hooks with steering and auto correction by a classifier step**
- `run some script conditioned on the agent's own decision` - the decision is forgotten and the main session/agent is none the wiser
  This allows for [agent-based-hooks](https://code.claude.com/docs/en/hooks#agent-based-hooks) but with the parent context, parent cache hit, and thus low overhead single-shot micro-fork type of agent based hook implementations. Making agent based hooks possibly viable, in contrast to the native implementation.

## Development

```sh
git clone https://github.com/NubeBuster/claude-gateway-extensions
cd claude-gateway-extensions
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
# or: uv sync --extra dev
make check   # ruff check, ruff format --check, pyright, pytest
make fix     # ruff format, ruff check --fix
```

Day-to-day work lands on `dev`; `main` receives only release merges (tags
`vX.Y.Z`), so it always points at released, working code.

The `cache` skill lives at `claude/skills/cache/SKILL.md` in the repo root;
`cgx configure` copies it into the target `skills/` directory. Cloned
instead of installed and want it without running `cgx configure`? Copy
`claude/skills/cache/` into `~/.claude/skills/` (or a project's
`.claude/skills/`) yourself.

MIT licensed, see `LICENSE`.
