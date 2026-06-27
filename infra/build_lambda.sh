#!/usr/bin/env bash
# Build the Lambda deployment package WITHOUT Docker.
#
# Installs the dependencies as Linux x86_64 / Python 3.12 wheels (the Lambda
# runtime) into infra/build/lambda/, then copies the pure-Python screener source
# in. boto3 is intentionally NOT bundled — the Lambda runtime already provides it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$HERE/build/lambda"

echo "Cleaning $OUT"
rm -rf "$OUT"
mkdir -p "$OUT"

echo "Installing Linux x86_64 / py3.12 dependency wheels..."
uv pip install \
  --target "$OUT" \
  --python-platform x86_64-manylinux2014 \
  --python-version 3.12 \
  --only-binary :all: \
  pydantic structlog requests tenacity

echo "Copying screener source..."
cp -r "$ROOT/src/screener" "$OUT/screener"

# Trim caches to keep the zip small.
find "$OUT" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT" -type d -name '*.dist-info' -prune -exec rm -rf {} + 2>/dev/null || true

echo "Built Lambda package at: $OUT"
du -sh "$OUT"
