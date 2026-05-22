---
name: Feature request
about: Propose a new capability or change in behavior
title: ""
labels: enhancement
assignees: ""
---

**The problem**
What are you trying to do? What's currently in the way?

**Proposed solution**
What would the feature look like from a user's perspective? Tool name, params,
example output.

**Alternatives considered**
Other approaches you've thought about and why you didn't pick them.

**Scope check**
This project is a caching MCP layer over the Strava API. Features that fit:

- New MCP tools that read or transform Strava data.
- Cache / sync improvements.
- Auth / deployment / docs improvements.

Features that probably don't fit (open an issue anyway, but expect pushback):

- Replicating arbitrary Strava write actions beyond the small set already
  supported (`sync_activities`, `delete_vault_activity`,
  `set_activity_location`).
- Non-Strava integrations.
- Hosted / multi-tenant variants.

**Additional context**
Screenshots, related issues, or links.
