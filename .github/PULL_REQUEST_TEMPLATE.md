<!--
Thanks for the PR! A few things that make review easier:
- Linked issue (if one exists)
- One logical change per PR
- Tests for new behavior; regression test for bug fixes
- CHANGELOG.md updated under [Unreleased]
-->

## Summary

What changes and why. Keep it short — the diff covers the *what*, the body
should cover the *why*.

## Linked issue

Fixes #

## Changes

- ...

## Test plan

- [ ] `pytest` passes locally
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] New behavior covered by a test (or N/A — explain why)
- [ ] CHANGELOG.md updated under `[Unreleased]` (or N/A — explain why)
- [ ] If touching Docker/compose: `docker build .` succeeds

## Notes for reviewers

Anything to call out — surprising decisions, deferred follow-ups, screenshots
for tool-output changes, etc.
