# P1 quality-tooling dependency rationale

## Scope

P1 adds three development-only tools to the `dev` dependency group. They are
not imported by the application runtime and do not add provider credentials,
provider writes, or a production service dependency.

| Tool | Locked version | P1 purpose | License evidence |
| --- | --- | --- | --- |
| `respx` | 0.23.1 | Deterministic HTTPX mocking for provider-contract tests; the harness blocks an unmocked test request. | Local frozen-install wheel metadata: `License: BSD-3-Clause`. |
| `ruff` | 0.16.6 | Bounded linting of new P1 modules only; it is not used for repository-wide formatting. | Local frozen-install wheel metadata: `License-Expression: MIT`. |
| `mypy` | 2.3.1 | Strict type checks for new P1 modules only; legacy `app.store` and legacy handler support remain explicit untyped boundaries. | Local frozen-install wheel metadata: `License-Expression: MIT`. |

The metadata above was read from the disposable Python 3.11 environment
created by `uv sync --frozen --python 3.11` for this P1 change.

## CVE and supply-chain posture

- The exact resolved versions are retained in `uv.lock`; CI installs only with
  `uv sync --frozen --python 3.11`.
- These packages are development-only. Runtime images and the FastAPI
  application dependency list are unchanged by this tooling addition.
- `respx` is used only to mock outbound HTTP in tests, so its first harness
  test has no live provider path.
- This repository has no configured vulnerability scanner in the P1 scope.
  Consequently, this document does not claim that a CVE scan found zero
  findings. A release or dependency-governance lane must run the approved SCA
  scanner against the locked dependency graph before production promotion.
