import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
PATTERN = re.compile(r"\bpk_(?:live|test)_[A-Za-z0-9\-_]{8,}_[A-Za-z0-9\-_]{16,}\b")


def should_skip(path: pathlib.Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def main() -> int:
    matches = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or should_skip(path):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".db"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if PATTERN.search(line):
                matches.append((path.relative_to(ROOT), lineno, line.strip()))
    if matches:
        print("Potential API key leak(s) found:")
        for relpath, lineno, line in matches:
            print(f"{relpath}:{lineno}: {line[:160]}")
        return 1
    print("No API key leak patterns detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
