"""Allow ``python -m claude_cache_bump_gateway.cli``."""

from __future__ import annotations

import sys

from claude_cache_bump_gateway.cli import main

if __name__ == "__main__":
    sys.exit(main())
