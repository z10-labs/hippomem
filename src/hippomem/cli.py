"""The `hippomem` command."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from hippomem import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hippomem", description="A team's engineering memory for AI coding agents.")
    parser.add_argument("--version", action="version", version=f"hippomem {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
