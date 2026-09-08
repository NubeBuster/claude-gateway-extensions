"""Allow ``python -m claude_gateway_extensions.cli``."""

from __future__ import annotations

import sys

from claude_gateway_extensions.cli import main

if __name__ == "__main__":
    sys.exit(main())
