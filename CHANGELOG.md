# Changelog

All notable changes to DirectPilot will be documented in this file.

The project follows semantic versioning during beta using `0.x.y` versions. Breaking changes may happen before `1.0.0`, but they should still be documented.

## [0.1.0-beta.1] - Unreleased

### Added

- Public beta repository governance files.
- Contribution, security, and safety-model documentation.
- GitHub issue and pull request templates.
- CI and security workflow definitions.
- Sanitized DirectPilot operations skill for agent-assisted workflows.

### Safety

- Documented `live_readonly` as the default beta mode.
- Documented write gates: `live_write`, `approved=true`, `idempotency_key`, and dry-run behavior.
