#!/usr/bin/env python3
"""Run kickforge auth with .env loaded from the repository root (fixes empty KICK_CLIENT_* when cwd/dotenv mismatch).

Usage (from anywhere):
  python scripts/kickforge_auth.py --channel <slug>
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("Install python-dotenv: pip install python-dotenv", file=sys.stderr)
        sys.exit(1)
    load_dotenv(repo_root / ".env")
    if not os.getenv("KICK_CLIENT_ID") or not os.getenv("KICK_CLIENT_SECRET"):
        print(
            f"KICK_CLIENT_ID / KICK_CLIENT_SECRET missing after loading {repo_root / '.env'}",
            file=sys.stderr,
        )
        sys.exit(1)
    raise SystemExit(subprocess.call(["kickforge", "auth", *sys.argv[1:]], env=os.environ.copy()))


if __name__ == "__main__":
    main()
