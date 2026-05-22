#!/usr/bin/env bash
# strava-mcp-vault — first-time setup
# Generates secrets, prompts for Strava credentials, writes .env.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  read -rp ".env already exists. Overwrite? [y/N] " yn
  case "${yn:-N}" in
    [Yy]*) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ first."
  exit 1
fi

py_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
py_major=${py_version%.*}
py_minor=${py_version#*.}
if (( py_major < 3 )) || (( py_major == 3 && py_minor < 10 )); then
  echo "ERROR: Python $py_version found, need 3.10+."
  echo "Try: brew install python@3.13   (macOS)"
  echo "  or: pyenv install 3.13        (any platform)"
  exit 1
fi

# Both keys generated from stdlib only — no third-party deps needed.
# Fernet keys are urlsafe-base64(32 random bytes); we match that format
# directly so users don't need 'cryptography' installed at setup time.
bearer=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
fernet=$(python3 -c "import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

echo
echo "=== Strava API credentials ==="
echo "Create an app at https://www.strava.com/settings/api"
echo "(Use a domain you own as the callback — it doesn't need a real web server.)"
echo
read -rp "  Client ID: " client_id
read -rp "  Client Secret: " client_secret

echo
echo "=== Initial OAuth tokens ==="
echo "See README -> 'OAuth: Get your access tokens' for the walkthrough."
echo "After first boot, the server refreshes tokens automatically."
echo
read -rp "  Access token: " access_token
read -rp "  Refresh token: " refresh_token

echo
echo "=== Endpoint security ==="
echo "The MCP endpoint exposes your private Strava data. The server refuses"
echo "to start unless one of these is set:"
echo "  - MCP_AUTH_TOKEN          (bearer-token auth — recommended)"
echo "  - MCP_ALLOW_UNAUTHENTICATED=1  (only safe on 127.0.0.1 or a trusted LAN)"
echo
read -rp "Enable bearer-token auth? [Y/n] " auth_yn
case "${auth_yn:-Y}" in
  [Nn]*)
    use_auth=0
    echo
    echo "⚠️  Unauthenticated mode selected. Make sure MCP_BIND_HOST stays on"
    echo "    127.0.0.1 or a trusted private network. Do NOT expose this"
    echo "    endpoint publicly without setting MCP_AUTH_TOKEN."
    ;;
  *)
    use_auth=1
    ;;
esac

# Build the auth section conditionally so the .env reflects the user's choice.
if [[ "$use_auth" == "1" ]]; then
  auth_block="# Bearer token for MCP endpoint authentication.
MCP_AUTH_TOKEN=\"$bearer\""
else
  auth_block="# Bearer auth disabled. Only safe when MCP_BIND_HOST is 127.0.0.1
# or a trusted private network. Set MCP_AUTH_TOKEN to re-enable.
MCP_ALLOW_UNAUTHENTICATED=1"
fi

# Write .env. We rebuild it minimally — see .env.example for all options.
cat > .env <<EOF
# Strava API credentials (from https://www.strava.com/settings/api)
STRAVA_CLIENT_ID="$client_id"
STRAVA_CLIENT_SECRET="$client_secret"

# Initial tokens from OAuth flow.
# After first boot, tokens are managed automatically in SQLite.
STRAVA_ACCESS_TOKEN="$access_token"
STRAVA_REFRESH_TOKEN="$refresh_token"

$auth_block

# Encrypts tokens at rest in SQLite. Save this somewhere safe —
# without it, the encrypted tokens are unrecoverable.
TOKEN_ENCRYPTION_KEY="$fernet"

# Uncomment to override defaults. See .env.example for the full list:
# STRAVA_MCP_PORT=18201
# MCP_BIND_HOST=127.0.0.1
# MCP_ALLOWED_ORIGINS=http://localhost:18201,https://claude.ai
EOF

chmod 600 .env

echo
echo "✅ .env created and chmod 600'd"
echo
echo "Save these somewhere safe (e.g. 1Password):"
echo
if [[ "$use_auth" == "1" ]]; then
  echo "  MCP_AUTH_TOKEN      (use in your client config)"
  echo "    $bearer"
  echo
fi
echo "  TOKEN_ENCRYPTION_KEY  (cannot be recovered if lost)"
echo "    $fernet"
echo
echo "Next:"
echo "  docker compose up -d                 # production (recommended)"
echo "  # or for local Python dev:"
echo "  python3 -m venv .venv && source .venv/bin/activate"
echo "  pip install -r requirements.txt && python -m strava_mcp_vault.server"
