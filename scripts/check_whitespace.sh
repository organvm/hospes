#!/usr/bin/env bash
set -euo pipefail

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

if git rev-parse --verify origin/main >/dev/null 2>&1; then
  git diff --check origin/main...HEAD
else
  empty_tree="$(git hash-object -t tree /dev/null)"
  git diff --check "$empty_tree" HEAD
fi
git diff --check
git diff --cached --check
