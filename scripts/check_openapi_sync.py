"""Fail if docs/openapi.json differs from the runtime FastAPI schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

SNAPSHOT = ROOT / "docs" / "openapi.json"


def normalise(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    runtime = json.loads(normalise(app.openapi()))
    if not SNAPSHOT.exists():
        print(f"missing OpenAPI snapshot: {SNAPSHOT}")
        return 1
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if normalise(runtime) != normalise(recorded):
        print("docs/openapi.json is out of sync with app.openapi().")
        print("Regenerate it after intentional API changes, then re-run this check.")
        return 1
    print("OpenAPI snapshot is in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
