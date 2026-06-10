# Agent Workflows for DirectPilot

This repository can be edited by humans or agent-assisted workflows. Agents must follow the same safety and review rules as human contributors.

## Role split

- Documentation changes: documentation engineer writes docs and templates.
- Code changes: coding agent implements only the requested change.
- Verification: verifier runs tests/builds and does not edit code.
- Review: reviewer checks diff, safety, and architecture risks and does not edit code.

## DirectPilot agent safety rules

Agents must not:

- read, print, copy, or commit real `.env` secrets;
- switch the default beta mode away from `live_readonly`;
- introduce live writes without explicit gates;
- remove `approved`, `idempotency_key`, or `dry_run` protections;
- claim a check passed without running it.

Agents should include real command output in PR descriptions when they run checks.

## Reusable skill

A sanitized public skill is available at `skills/directpilot-operations/SKILL.md`. It documents the safe operating contract without embedding private profile memory, tokens, customer data, or local machine paths.
