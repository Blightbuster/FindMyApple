"""Test data that may be real: read from the uncommitted `.env` at the repo root.

Real values (your tag's name, where it was seen) live only in `.env`, which git ignores and the
pre-commit hook refuses. Without it (e.g. in CI) the made-up defaults below are used, so tests
never depend on anyone's data. Environment variables override `.env`. Keys: see `.env.example`.
"""

from __future__ import annotations

import os
from pathlib import Path

from homeassistant.util import slugify

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


_ENV = {**_load_env(ENV_FILE), **os.environ}


def _get(key: str, default: str) -> str:
    return _ENV.get(key) or default


TAG_NAME = _get("FMA_TEST_TAG_NAME", "Keys")
TAG_MODEL = _get("FMA_TEST_TAG_MODEL", "PAD Tag")
TAG_LATITUDE = float(_get("FMA_TEST_TAG_LATITUDE", "52.52"))
TAG_LONGITUDE = float(_get("FMA_TEST_TAG_LONGITUDE", "13.405"))
TAG_ACCURACY = int(_get("FMA_TEST_TAG_ACCURACY", "72"))

TAG_SLUG = slugify(TAG_NAME)  # entity ids are derived from the name
USING_REAL_DATA = ENV_FILE.is_file()
