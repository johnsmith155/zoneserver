#!/usr/bin/env bash
#
# Deploys the edge worker. Run from a machine with Node (the laptop):
#
#   bash edge/deploy.sh
#
# Secrets never pass through this terminal: the Cloudflare token is read from
# ~/.cloudflare-token (saved by ~/cloudflare-setup.sh), and the two worker keys
# are generated once into ~/.zoneedge-publish-key and ~/.zoneedge-read-key and
# handed to wrangler on stdin.
set -euo pipefail
cd "$(dirname "$0")"

[ -s "$HOME/.cloudflare-token" ] || { echo "Run ~/cloudflare-setup.sh first."; exit 1; }
export CLOUDFLARE_API_TOKEN
CLOUDFLARE_API_TOKEN="$(cat "$HOME/.cloudflare-token")"
export WRANGLER_SEND_METRICS=false
W="npx --yes wrangler@4"

# The schema is idempotent (CREATE ... IF NOT EXISTS).
$W d1 execute zone-reports --remote --file schema.sql --yes >/dev/null
echo "schema: applied"

$W deploy 2>&1 | grep -E "Uploaded|Published|https://|Deployed" || true

for name in PUBLISH_KEY READ_KEY; do
  file="$HOME/.zoneedge-$(echo "$name" | tr 'A-Z_' 'a-z-' | sed 's/-key$//')-key"
  if [ ! -s "$file" ]; then
    umask 077
    python -c "import secrets,sys; sys.stdout.write(secrets.token_urlsafe(32))" > "$file"
    chmod 600 "$file" 2>/dev/null || true
  fi
  $W secret put "$name" < "$file" >/dev/null
  echo "secret $name: set (from $file)"
done
