# Contributing

Thanks for considering a contribution. This project is a personal scratch-an-itch
that grew up; PRs that fit the project's narrow scope (caching MCP layer over
the Strava API) are welcome.

## Before you start

- For non-trivial changes, please open an issue first to discuss the approach.
  This keeps PRs from getting closed because they don't fit the scope or
  conflict with planned work.
- See [`ROADMAP.md`](ROADMAP.md) for ideas already on the table.

## Dev setup

Requires Python 3.10 or newer.

```bash
git clone https://github.com/<owner>/strava-mcp-vault.git
cd strava-mcp-vault
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# Fill in STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, tokens — see README
```

Run the server locally:

```bash
VAULT_DB_PATH=./data/vault.db python -m strava_mcp_vault.server
```

## Tests, lint, format

```bash
pytest                              # full suite (asyncio mode auto)
pytest --cov --cov-report=term-missing   # with coverage (>=70% gate)
ruff check .                        # lint
ruff format --check .               # formatting check (use without --check to apply)
```

CI runs all three on every PR against `main` across Python 3.10–3.13, plus a
Docker build. PRs are expected to be green before review.

## Commit conventions

Commits follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(scope): short summary
fix(scope): short summary
docs(scope): short summary
refactor(scope): short summary
chore(scope): short summary
```

Examples drawn from this repo's history:

- `feat(mcp): add response_format=json and pagination to list tools`
- `fix(server): default bind to loopback outside Docker`
- `docs(readme): correct refresh-token rotation warning`

Multi-line bodies should explain *why*, not *what* — the diff already covers
*what*.

## Pull requests

- Branch from `main`. Rebase before opening if `main` has moved.
- Keep PRs focused — one logical change per PR. Drive-by cleanups belong in
  their own commits, ideally their own PRs.
- Update [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]` for any
  user-visible change.
- Add tests for new behavior. Bug fixes should add a regression test that
  fails without the fix.

## Reporting bugs

Use the bug report template under [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/).
Please include logs (`docker logs strava-mcp-vault` or the server's stderr),
the MCP client you're using, and the minimal steps to reproduce.

For anything involving credentials, tokens, or the auth layer, please follow
[`SECURITY.md`](SECURITY.md) and report privately instead.
