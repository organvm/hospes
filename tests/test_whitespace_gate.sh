#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT

git -C "$test_root" init -q
git -C "$test_root" config user.email "synthetic@example.test"
git -C "$test_root" config user.name "Synthetic Test"
printf 'clean\n' > "$test_root/fixture.txt"
git -C "$test_root" add fixture.txt
git -C "$test_root" commit -qm "test: clean root"

git -C "$test_root" remote | grep -q . && {
  echo "whitespace fallback fixture unexpectedly has a remote" >&2
  exit 1
}
(cd "$test_root" && bash "$repo_root/scripts/check_whitespace.sh")

printf 'committed trailing whitespace   \n' > "$test_root/fixture.txt"
git -C "$test_root" add fixture.txt
git -C "$test_root" commit -qm "test: bad committed whitespace"
if (cd "$test_root" && bash "$repo_root/scripts/check_whitespace.sh"); then
  echo "whitespace fallback accepted committed trailing whitespace" >&2
  exit 1
fi

echo "whitespace fallback predicates: ok"
