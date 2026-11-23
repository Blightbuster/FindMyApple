#!/bin/sh
# Run lint + tests in Linux (Home Assistant doesn't run on Windows). Usage: sh scripts_test_docker.sh
set -e
cd /repo
pip install -q --root-user-action=ignore uv && uv pip install -q --system -r requirements_test.txt
ruff check --no-cache .
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider "$@"
