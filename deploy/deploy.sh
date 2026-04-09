#!/usr/bin/env bash
#
# One-command DigitalOcean deploy for tennis-arb-bot.
#
# Prereqs (one-time, on your machine):
#   1. A local .env file at the repo root with your real Polymarket creds
#      (POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE, POLY_PRIVATE_KEY)
#   2. An SSH keypair at ~/.ssh/id_ed25519(.pub) — generate one with:
#        ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
#   3. A DigitalOcean API token with read+write scope:
#        https://cloud.digitalocean.com/account/api/tokens
#      Provide it ONE of these ways (in priority order):
#        a. export DIGITALOCEAN_TOKEN=dop_v1_xxx
#        b. write the token to deploy/.do-token (gitignored)
#
# Usage:
#   ./deploy/deploy.sh
#
# Environment overrides (optional):
#   DROPLET_NAME    default: tennis-arb-bot
#   DROPLET_REGION  default: nyc3
#   DROPLET_SIZE    default: s-1vcpu-1gb  ($6/mo, plenty for this bot)
#   SSH_KEY_PATH    default: ~/.ssh/id_ed25519

set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

# --- 1. Prereq checks ----------------------------------------------------
[ -f .env ] || {
  echo "ERROR: .env not found at repo root."
  echo "Create one by copying .env.example and filling in your real Polymarket credentials."
  exit 1
}
[ -f sessions.jsonl ] || {
  echo "ERROR: sessions.jsonl not found at repo root."
  echo "Nitter requires at least one valid X session (JSONL format) to fetch tweets."
  echo "Place authenticated X account cookies in sessions.jsonl before deploying."
  exit 1
}
command -v curl >/dev/null || { echo "ERROR: curl is required"; exit 1; }
command -v ssh  >/dev/null || { echo "ERROR: ssh is required";  exit 1; }
command -v scp  >/dev/null || { echo "ERROR: scp is required";  exit 1; }
command -v tar  >/dev/null || { echo "ERROR: tar is required";  exit 1; }
command -v python3 >/dev/null || command -v python >/dev/null || {
  echo "ERROR: python3 (or python) is required for JSON handling"; exit 1;
}
PY=$(command -v python3 || command -v python)

TOKEN="${DIGITALOCEAN_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f deploy/.do-token ]; then
  TOKEN=$(tr -d '[:space:]' < deploy/.do-token)
fi
[ -n "$TOKEN" ] || {
  echo "ERROR: No DigitalOcean token. Either:"
  echo "  export DIGITALOCEAN_TOKEN=dop_v1_xxx"
  echo "  or write the token to deploy/.do-token"
  exit 1
}

DROPLET_NAME="${DROPLET_NAME:-tennis-arb-bot}"
REGION="${DROPLET_REGION:-nyc3}"
SIZE="${DROPLET_SIZE:-s-1vcpu-1gb}"
IMAGE="ubuntu-24-04-x64"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519}"
[ -f "${SSH_KEY_PATH}.pub" ] || {
  echo "ERROR: No SSH public key at ${SSH_KEY_PATH}.pub"
  echo "Generate one with: ssh-keygen -t ed25519 -f $SSH_KEY_PATH"
  exit 1
}

API="https://api.digitalocean.com/v2"
AUTH="Authorization: Bearer $TOKEN"
SSH_OPTS=(-i "$SSH_KEY_PATH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)

# --- 2. Upload SSH key to DigitalOcean (idempotent by name) --------------
# Override SSH_KEY_NAME if you deploy from multiple machines under the same
# username — otherwise the second machine reuses the first machine's key
# entry by name and the new pubkey never gets uploaded.
SSH_KEY_NAME="${SSH_KEY_NAME:-tennis-arb-bot-deploy-$(whoami 2>/dev/null || echo user)}"
echo "[1/6] Ensuring SSH key '$SSH_KEY_NAME' is registered with DigitalOcean..."

KEY_ID=$(curl -sSf -H "$AUTH" "$API/account/keys?per_page=200" \
  | "$PY" -c "
import sys, json, os
ks = json.load(sys.stdin).get('ssh_keys', [])
print(next((k['id'] for k in ks if k['name']==os.environ['SSH_KEY_NAME']), ''))
" SSH_KEY_NAME="$SSH_KEY_NAME" 2>/dev/null || true)

if [ -z "$KEY_ID" ]; then
  PUBKEY=$(cat "${SSH_KEY_PATH}.pub")
  PAYLOAD=$(SSH_KEY_NAME="$SSH_KEY_NAME" PUBKEY="$PUBKEY" "$PY" -c "
import json, os
print(json.dumps({'name': os.environ['SSH_KEY_NAME'], 'public_key': os.environ['PUBKEY']}))
")
  KEY_ID=$(curl -sSf -X POST -H "$AUTH" -H "Content-Type: application/json" \
    "$API/account/keys" -d "$PAYLOAD" \
    | "$PY" -c "import sys, json; print(json.load(sys.stdin)['ssh_key']['id'])")
  echo "      Uploaded new key (id $KEY_ID)"
else
  echo "      Reusing existing key (id $KEY_ID)"
fi

# --- 3. Reuse existing droplet by name, or create one --------------------
echo "[2/6] Looking up droplet '$DROPLET_NAME'..."
DROPLET_ID=$(curl -sSf -H "$AUTH" "$API/droplets?tag_name=tennis-arb-bot&per_page=200" \
  | DROPLET_NAME="$DROPLET_NAME" "$PY" -c "
import sys, json, os
ds = json.load(sys.stdin).get('droplets', [])
print(next((str(d['id']) for d in ds if d['name']==os.environ['DROPLET_NAME']), ''))
")

if [ -z "$DROPLET_ID" ]; then
  echo "      Creating new droplet ($SIZE in $REGION)..."
  PAYLOAD=$(DROPLET_NAME="$DROPLET_NAME" REGION="$REGION" SIZE="$SIZE" IMAGE="$IMAGE" KEY_ID="$KEY_ID" "$PY" -c "
import json, os
with open('deploy/cloud-init.yaml') as f:
    user_data = f.read()
print(json.dumps({
    'name': os.environ['DROPLET_NAME'],
    'region': os.environ['REGION'],
    'size': os.environ['SIZE'],
    'image': os.environ['IMAGE'],
    'ssh_keys': [int(os.environ['KEY_ID'])],
    'user_data': user_data,
    'tags': ['tennis-arb-bot'],
    'monitoring': True,
    'ipv6': False,
    'backups': False,
}))
")
  DROPLET_ID=$(curl -sSf -X POST -H "$AUTH" -H "Content-Type: application/json" \
    "$API/droplets" -d "$PAYLOAD" \
    | "$PY" -c "import sys, json; print(json.load(sys.stdin)['droplet']['id'])")
  echo "      Created droplet id $DROPLET_ID"
else
  echo "      Reusing existing droplet id $DROPLET_ID"
fi

# --- 4. Wait for IP + SSH + cloud-init -----------------------------------
echo "[3/6] Waiting for droplet network..."
IP=""
for i in $(seq 1 60); do
  IP=$(curl -sSf -H "$AUTH" "$API/droplets/$DROPLET_ID" \
    | "$PY" -c "
import sys, json
d = json.load(sys.stdin)['droplet']
nets = d.get('networks', {}).get('v4', [])
print(next((n['ip_address'] for n in nets if n['type']=='public'), ''))
")
  [ -n "$IP" ] && break
  sleep 5
done
[ -n "$IP" ] || { echo "ERROR: Timed out waiting for droplet IP"; exit 1; }
echo "      Public IP: $IP"

echo "[4/6] Waiting for SSH + cloud-init to finish (this can take 2-3 min)..."
for i in $(seq 1 60); do
  if ssh "${SSH_OPTS[@]}" "root@$IP" "test -f /var/lib/cloud/instance/boot-finished" 2>/dev/null; then
    echo "      cloud-init done."
    break
  fi
  sleep 5
done

# --- 5. Ship code + .env + sessions.jsonl --------------------------------
echo "[5/6] Shipping project files + .env + sessions.jsonl..."
TARBALL=$(mktemp -t tennis-arb-bot.XXXXXX.tar.gz)
trap 'rm -f "$TARBALL"' EXIT
tar --exclude='./.git' \
    --exclude='./__pycache__' \
    --exclude='./*.pyc' \
    --exclude='./venv' \
    --exclude='./.venv' \
    --exclude='./.env' \
    --exclude='./sessions.jsonl' \
    --exclude='./sessions.json' \
    --exclude='./deploy/.do-token' \
    -czf "$TARBALL" -C . .
scp "${SSH_OPTS[@]}" "$TARBALL" "root@$IP:/tmp/tennis-arb-bot.tar.gz"
scp "${SSH_OPTS[@]}" .env "root@$IP:/tmp/tennis-arb-bot.env"
scp "${SSH_OPTS[@]}" sessions.jsonl "root@$IP:/tmp/tennis-arb-bot.sessions.jsonl"

# --- 6. Install + start --------------------------------------------------
echo "[6/6] Installing dependencies, starting Nitter + bot..."
ssh "${SSH_OPTS[@]}" "root@$IP" bash <<'REMOTE'
set -euo pipefail

# Stop the bot so we're not racing it during install. We do NOT stop the
# Nitter containers — we want them to keep serving across redeploys, and
# `docker compose up -d` is idempotent on unchanged services.
systemctl stop tennis-arb-bot 2>/dev/null || true

# Unpack code
rm -rf /opt/tennis-arb-bot.new
mkdir -p /opt/tennis-arb-bot.new
tar -xzf /tmp/tennis-arb-bot.tar.gz -C /opt/tennis-arb-bot.new
rm /tmp/tennis-arb-bot.tar.gz

# Atomic-ish swap: keep venv across deploys to avoid reinstalling deps every time
if [ -d /opt/tennis-arb-bot/venv ]; then
  mv /opt/tennis-arb-bot/venv /opt/tennis-arb-bot.new/venv
fi
# Preserve any previously-generated nitter.conf with a real hmac key.
# (See "First-deploy hmac key generation" below — once patched, the file
# carries a randomized secret we don't want to overwrite from the tarball.)
if [ -f /opt/tennis-arb-bot/nitter.conf ] \
   && ! grep -q 'CHANGE_ME_TO_A_LONG_RANDOM_SECRET' /opt/tennis-arb-bot/nitter.conf; then
  cp /opt/tennis-arb-bot/nitter.conf /opt/tennis-arb-bot.new/nitter.conf
fi
rm -rf /opt/tennis-arb-bot
mv /opt/tennis-arb-bot.new /opt/tennis-arb-bot

# Install .env + sessions.jsonl with restrictive perms
mv /tmp/tennis-arb-bot.env          /opt/tennis-arb-bot/.env
mv /tmp/tennis-arb-bot.sessions.jsonl /opt/tennis-arb-bot/sessions.jsonl
chown -R bot:bot /opt/tennis-arb-bot
chmod 600 /opt/tennis-arb-bot/.env /opt/tennis-arb-bot/sessions.jsonl

# First-deploy hmac key generation: replace the placeholder in nitter.conf
# with a real random secret. Done in-place so subsequent deploys (which
# preserve nitter.conf above) keep the same key — Nitter caches things
# under it, so changing the key invalidates state needlessly.
if grep -q 'CHANGE_ME_TO_A_LONG_RANDOM_SECRET' /opt/tennis-arb-bot/nitter.conf; then
  HMAC_KEY=$(openssl rand -hex 32)
  sed -i "s|CHANGE_ME_TO_A_LONG_RANDOM_SECRET|${HMAC_KEY}|" /opt/tennis-arb-bot/nitter.conf
  echo "      Generated new Nitter hmac key"
fi

# Build / refresh venv
sudo -u bot bash -c '
  set -e
  cd /opt/tennis-arb-bot
  if [ ! -d venv ]; then
    python3 -m venv venv
  fi
  ./venv/bin/pip install --quiet -U pip
  ./venv/bin/pip install --quiet -r requirements.txt
'

# Bring up Nitter + Redis. Idempotent: unchanged services stay running.
echo "      Starting Nitter stack..."
cd /opt/tennis-arb-bot
docker compose up -d

# Wait for Nitter to actually serve. First boot can take 30-60s while it
# warms its session pool against X. We don't fail the deploy if Nitter
# isn't ready in time — XSource degrades gracefully and falls through to
# Sofascore/ESPN — but we log a clear warning.
echo "      Waiting for Nitter on http://127.0.0.1:8080 ..."
NITTER_OK=0
for i in $(seq 1 30); do
  if curl -sf -o /dev/null --max-time 3 http://127.0.0.1:8080/EntryLists; then
    NITTER_OK=1
    echo "      Nitter is up."
    break
  fi
  sleep 2
done
if [ "$NITTER_OK" -ne 1 ]; then
  echo "      WARNING: Nitter did not respond within 60s. Last container logs:"
  docker logs --tail 30 nitter || true
  echo "      Bot will start anyway — XSource will fall back to Sofascore/ESPN."
fi

# Install + (re)start systemd unit
cp /opt/tennis-arb-bot/deploy/tennis-arb-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable tennis-arb-bot >/dev/null
systemctl restart tennis-arb-bot

sleep 2
systemctl --no-pager status tennis-arb-bot | head -20 || true
REMOTE

cat <<EOF

============================================================
  Deploy complete.
============================================================
  Droplet:  $DROPLET_NAME ($DROPLET_ID)  $IP

  Tail logs:
    ssh -i $SSH_KEY_PATH root@$IP 'journalctl -u tennis-arb-bot -f'

  Restart:
    ssh -i $SSH_KEY_PATH root@$IP 'systemctl restart tennis-arb-bot'

  Re-deploy after code changes:
    ./deploy/deploy.sh

  Destroy droplet:
    curl -X DELETE -H "Authorization: Bearer \$DIGITALOCEAN_TOKEN" \\
      https://api.digitalocean.com/v2/droplets/$DROPLET_ID
============================================================
EOF
