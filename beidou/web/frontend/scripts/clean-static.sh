#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
git rm -rf ../static/assets 2>/dev/null || true
rm -f ../static/index.html
