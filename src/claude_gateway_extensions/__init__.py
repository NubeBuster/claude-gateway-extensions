"""claude-gateway-extensions: keep a Claude Code session's prompt cache warm.

A small transparent HTTP proxy between Claude Code and the Anthropic API that
records the last request of each session and periodically replays it with
``max_tokens: 0`` so the server-side prompt cache does not expire while the
user is away. See ``docs/DESIGN.md`` for the design decisions.
"""

__version__ = "0.1.0"
