"""Pre-commit guard: no personal data, no artifacts.

Blocks a commit when a staged file
  - is an .env file (other than .env.example), spike state, a log, CoreADI, a session or key file;
  - is larger than 1 MB (artifacts, not source);
  - contains any value from the local .env (your real tag names, coordinates, ...), except values
    that also appear in .env.example (public defaults).
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


def staged_blob(path: str) -> bytes:
    return subprocess.run(["git", "show", f":{path}"], capture_output=True, check=True, cwd=ROOT).stdout


def _values(path: Path) -> list[str]:
    if not path.is_file():
        return []
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            values.append(line.partition("=")[2].strip().strip("'\""))
    return values


def env_values() -> list[str]:
    """Values from .env worth guarding: not too short to match everywhere, not public defaults."""
    public = set(_values(ROOT / ".env.example"))
    return [v for v in _values(ROOT / ".env") if len(v) >= 4 and v not in public]


def main() -> int:
    problems = []
    secrets = env_values()
    for path in staged():
        if path not in ALLOWED and any(re.search(p, path) for p in BLOCKED):
            problems.append(f"{path}: personal data or artifact path, never committed")
            continue
        blob = staged_blob(path)
        if len(blob) > MAX_BYTES and not path.endswith(".lock"):
            problems.append(f"{path}: {len(blob):,} bytes, looks like an artifact")
        text = blob.decode("utf-8", errors="ignore")
        for value in secrets:
            if value in text:
                problems.append(f"{path}: contains a value from your .env ({value[:2]}…)")
    if problems:
        print("pre-commit: refusing to commit:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
