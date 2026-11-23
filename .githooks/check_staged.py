"""Pre-commit guard: no personal data, no artifacts.

Blocks a commit when a staged file
  - is an .env file (other than .env.example), spike state, a log, CoreADI, a session or key file;
  - is larger than 1 MB (artifacts, not source).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKED = [
    r"(^|/)\.env(\..+)?$",
    r"(^|/)state/",
    r"\.log$",
    r"(^|/)CoreADI",
    r"\.partial$",
    r"(^|/)account\.json$",
    r"(^|/)accessories/",
    r"(^|/)emulated-mac/",
    r"\.plist$",
    r"(^|/)third_party/",
]
ALLOWED = {".env.example"}
MAX_BYTES = 1_000_000


def staged() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        capture_output=True, check=True, cwd=ROOT,
    ).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def staged_size(path: str) -> int:
    out = subprocess.run(["git", "cat-file", "-s", f":{path}"], capture_output=True, check=True, cwd=ROOT)
    return int(out.stdout)


def main() -> int:
    problems = []
    for path in staged():
        if path not in ALLOWED and any(re.search(p, path) for p in BLOCKED):
            problems.append(f"{path}: personal data or artifact path, never committed")
            continue
        size = staged_size(path)
        if size > MAX_BYTES and not path.endswith(".lock"):
            problems.append(f"{path}: {size:,} bytes, looks like an artifact")
    if problems:
        print("pre-commit: refusing to commit:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
