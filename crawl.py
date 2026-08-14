#!/usr/bin/env python3
"""AUSCrawl entry point.

The implementation lives in the auscrawl package; this shim keeps the documented
`uv run python crawl.py ...` commands working.
"""

from auscrawl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
