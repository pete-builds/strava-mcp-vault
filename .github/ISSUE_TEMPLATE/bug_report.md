---
name: Bug report
about: Something broken or behaving unexpectedly
title: ""
labels: bug
assignees: ""
---

**Describe the bug**
A clear, concise description of what's going wrong.

**To reproduce**
Steps to reproduce the behavior:
1. ...
2. ...
3. See error

**Expected behavior**
What you expected to happen instead.

**Logs**
```
paste relevant output from `docker logs strava-mcp-vault` or the server's stderr
```
Please scrub any tokens, client secrets, or personal Strava IDs.

**Environment**
- strava-mcp-vault version / commit:
- MCP client (Claude Desktop / Claude Code / Claude.ai web / Cowork / other):
- Deployment (Docker / local Python / other):
- Host OS:
- Python version (if running locally):

**Additional context**
Anything else relevant — recent config changes, related issues, what you've
already tried.

> ⚠️ For security issues (auth bypass, token leakage, etc.), please follow
> [`SECURITY.md`](../../SECURITY.md) and report privately instead.
