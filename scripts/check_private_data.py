#!/usr/bin/env python3
"""Fail when newly tracked HOSPES work resembles secrets or private guest data."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "OpenAI-style token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ALLOWED_EMAIL_SUFFIXES = ("@example.com", "@example.org", "@example.net", "@example.test")


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return completed.stdout


def added_lines(*, cached: bool = False) -> list[tuple[str, str]]:
    if cached:
        diff = git("diff", "--cached", "--unified=0")
    else:
        try:
            diff = git("diff", "--unified=0", "origin/main...HEAD")
        except subprocess.CalledProcessError:
            diff = git("diff", "--unified=0", "HEAD~1...HEAD")
    path = "<unknown>"
    lines: list[tuple[str, str]] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            lines.append((path, line[1:]))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cached",
        action="store_true",
        help="scan additions in the staged index instead of the committed topic range",
    )
    args = parser.parse_args(argv)
    found_private_data = False
    for _path, line in added_lines(cached=args.cached):
        for _label, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                found_private_data = True
        for address in EMAIL.findall(line):
            if not address.lower().endswith(ALLOWED_EMAIL_SUFFIXES):
                found_private_data = True
    if found_private_data:
        # Never echo a matched value or any input-derived context. The scanner
        # itself must not become a clear-text exfiltration surface in CI logs.
        print(
            "privacy scan: FAIL: added diff contains potential private data; "
            "inspect locally without logging matched content",
            file=sys.stderr,
        )
        return 1
    print("privacy scan: ok (no added secrets or non-example email addresses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
