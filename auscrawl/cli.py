"""Command line entry point."""

import argparse
import asyncio
import logging

from rich.logging import RichHandler

from . import config
from .pipeline import run

RATE_CEILING = 30.0


def _rate(value: str) -> float:
    r = float(value)
    if not 0 < r <= RATE_CEILING:
        raise argparse.ArgumentTypeError(
            f"rate must be between 0 and {RATE_CEILING} req/s")
    return r


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawl.py",
        description="Crawl AUS Banner 9 course data into SQLite.",
    )
    p.add_argument("-o", "--output", default="aus_data.db",
                   help="output database (default: aus_data.db; the shipped "
                        "snapshot aus_courses.db is deliberately not the default)")
    p.add_argument("-t", "--terms", nargs="+", metavar="TERM",
                   help="specific term ids, e.g. 202620 202510")
    p.add_argument("--latest", action="store_true",
                   help="crawl only the newest term")
    p.add_argument("--resume", action="store_true",
                   help="skip terms and course versions already stored")
    p.add_argument("--force", action="store_true",
                   help="delete the database and start over")
    p.add_argument("--no-catalog", action="store_true",
                   help="skip the catalog phase (and details, which depend on it)")
    p.add_argument("--no-details", action="store_true",
                   help="skip the per-course detail phase")
    p.add_argument("--rate", type=_rate, default=config.DEFAULT_RATE,
                   help=f"target requests per second (default: {config.DEFAULT_RATE})")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", handlers=[RichHandler(rich_tracebacks=True)],
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logging.getLogger("auscrawl").warning("interrupted; progress is committed")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
