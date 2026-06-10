"""Lightweight repository secret guard for public beta PRs.

This is not a replacement for GitHub secret scanning or gitleaks. It catches
common accidents in tracked text files and intentionally ignores local `.env`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_NAMES = {"uv.lock"}
ASSIGNMENT = re.compile(
    r"\b[A-Z0-9_]*(TOKEN|SECRET|API_KEY|CLIENT_SECRET)[A-Z0-9_]*\b\s*=\s*([^\s#]+)"
)
PATTERNS = [
    re.compile(r"(?i)authorization\s*:\s*(oauth|bearer)\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"AQVN(?!-SECRET)[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]
ALLOWLIST_PREFIXES = (
    "<",
    "${",
    "example",
    "changeme",
    "redacted",
    "your-",
    "test",
    "topsecret",
    "aqvn-secret",
    "fake",
    "dummy",
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        text=True,
        check=True,
        capture_output=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def is_skipped(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_PARTS for part in rel.parts):
        return True
    if path.name in SKIP_NAMES:
        return True
    if path.name == ".env.example":
        return True
    return False


def suspicious_assignment_value(line: str) -> bool:
    match = ASSIGNMENT.search(line)
    if not match:
        return False
    raw = match.group(2).strip().strip("\"',)")
    lowered = raw.lower()
    if not raw or lowered.startswith(ALLOWLIST_PREFIXES):
        return False
    if any(marker in lowered for marker in ("settings.", "os.getenv", "getenv", "none", "true", "false")):
        return False
    return len(raw) >= 20 and " " not in raw


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if is_skipped(path) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if suspicious_assignment_value(line):
                findings.append(f"{path.relative_to(ROOT)}:{lineno}: possible secret-like value")
                continue
            for pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{lineno}: possible secret-like value")
                    break
    if findings:
        print("Potential secrets detected:")
        for item in findings:
            print(f"- {item}")
        return 1
    print("No obvious tracked secrets detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
