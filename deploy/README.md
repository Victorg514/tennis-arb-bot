# Deploying tennis-arb-bot to DigitalOcean

One-command deploy to a fresh Ubuntu droplet. After this is set up, every
re-run of `deploy.sh` updates the code on the droplet in place — same
droplet, no downtime beyond a systemd restart.

## One-time setup

1. **Create your `.env` at the repo root** with real Polymarket credentials:

   ```bash
   cp .env.example .env
   # then edit .env and fill in:
   #   POLY_API_KEY
   #   POLY_API_SECRET
   #   POLY_API_PASSPHRASE
   #   POLY_PRIVATE_KEY  (the wallet that will sign trades)
   ```

2. **Make sure you have an SSH keypair** at `~/.ssh/id_ed25519`:

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
   ```

3. **Get a DigitalOcean API token** with read+write scope from
   https://cloud.digitalocean.com/account/api/tokens

   Then provide it one of two ways:

   ```bash
   export DIGITALOCEAN_TOKEN=dop_v1_xxxxxxxx
   ```

   or write it to `deploy/.do-token` (gitignored):

   ```bash
   echo 'dop_v1_xxxxxxxx' > deploy/.do-token
   ```

## Deploy

```bash
./deploy/deploy.sh
```

That's it. The script will:

1. Register your SSH key with DigitalOcean (idempotent — only first time)
2. Create a `s-1vcpu-1gb` Ubuntu 24.04 droplet in `nyc3` (~$6/mo) — or
   reuse an existing one named `tennis-arb-bot`
3. Wait for cloud-init to install Python, build-essential, and create the
   unprivileged `bot` user
4. Tar the project (excluding `.git`, `venv`, `.env`, etc.) and `scp` it
   plus your local `.env` to the droplet
5. Build a venv and `pip install -r requirements.txt` on the droplet
6. Install and start the `tennis-arb-bot` systemd service

The bot now runs 24/7. systemd restarts it on failure with a 10s backoff.

## Useful commands after deploy

The script prints the right SSH command at the end. The patterns are:

```bash
# Tail logs
ssh -i ~/.ssh/id_ed25519 root@DROPLET_IP 'journalctl -u tennis-arb-bot -f'

# Restart
ssh -i ~/.ssh/id_ed25519 root@DROPLET_IP 'systemctl restart tennis-arb-bot'

# Stop
ssh -i ~/.ssh/id_ed25519 root@DROPLET_IP 'systemctl stop tennis-arb-bot'

# Check status
ssh -i ~/.ssh/id_ed25519 root@DROPLET_IP 'systemctl status tennis-arb-bot'
```

## Re-deploying

Just run `./deploy/deploy.sh` again. It will:
- Reuse the existing droplet (matched by name)
- Re-upload the code
- Reuse the existing venv across deploys (so subsequent deploys are fast —
  only `pip install` for changed deps)
- Restart the systemd service

## Customizing

Override these via environment variables before running the script:

| Variable | Default | Notes |
|---|---|---|
| `DROPLET_NAME` | `tennis-arb-bot` | Used to find/reuse the droplet |
| `DROPLET_REGION` | `nyc3` | Any DO region slug |
| `DROPLET_SIZE` | `s-1vcpu-1gb` | $6/mo. Plenty for this bot. |
| `SSH_KEY_PATH` | `~/.ssh/id_ed25519` | Path to your private key (the script reads `.pub` for upload) |

## Tearing it down

```bash
# Get the droplet id from the last deploy output, then:
curl -X DELETE -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  https://api.digitalocean.com/v2/droplets/DROPLET_ID
```

Or destroy from the DigitalOcean web UI.

## What's NOT in the deploy

- **No HTTPS / public ports.** The bot only makes outbound HTTPS calls to
  Polymarket. Nothing listens on the droplet, so no firewall config is
  needed.
- **No backups.** The droplet is stateless — your code lives in this repo,
  your secrets live in `.env` on your machine. If the droplet dies, just
  re-run `deploy.sh` and it rebuilds.
- **No monitoring beyond DO's built-in droplet metrics.** Logs go to
  systemd's journal; check them with `journalctl -u tennis-arb-bot`.
