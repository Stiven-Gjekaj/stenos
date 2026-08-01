"""Entry point for `python -m stenos` and for the frozen executable."""

from __future__ import annotations

import sys

from stenos.bot import main

if __name__ == "__main__":
    sys.exit(main())
