# Deploy the Polymarket bot to a Hetzner VPS (Helsinki)

Render's datacenters are on Polymarket's geoblock list. Hetzner's **Helsinki (Finland)** region is not, so we run the bot from a €4/mo Hetzner Cloud server instead. The bot code is identical — only the host changes.

Total time: ~15 minutes. Total cost: €3.79/mo (CX22 in Helsinki).

---

## 1. Create the Hetzner server

1. Sign up at [console.hetzner.cloud](https://console.hetzner.cloud) (email + card; no KYC).
2. **New project** → name it whatever (e.g. `polymarket`).
3. **Add server**:
   - **Location**: **Helsinki (hel1)** ← critical, not Falkenstein/Nuremberg which are in Germany and blocked
   - **Image**: Ubuntu 24.04
   - **Type**: **CX22** (€3.79/mo, 2 vCPU, 4 GB RAM — way more than we need)
   - **Networking**: IPv4 + IPv6 both on
   - **SSH keys**: add yours if you have one; otherwise Hetzner emails you a root password
   - **Name**: `polymarket-bot`
   - Click **Create & Buy now**
4. Server is ready in ~30 seconds. Note the public IPv4 address.

---

## 2. First-login hardening (2 minutes)

SSH in:

```bash
ssh root@YOUR_SERVER_IP
```

Update the box and create a non-root user:

```bash
apt update && apt upgrade -y
adduser bot          # set a password; answer the prompts (defaults are fine)
usermod -aG sudo bot
# Copy your SSH key so you can log in as `bot` directly
rsync --archive --chown=bot:bot ~/.ssh /home/bot
```

Log out, then log back in as `bot`:

```bash
exit
ssh bot@YOUR_SERVER_IP
```

---

## 3. Install Python + pull the bot code

```bash
sudo apt install -y python3.12 python3.12-venv git
git clone https://github.com/bigseanny/polymarket-bot.git
cd polymarket-bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Configure the environment

Copy the template and fill it in:

```bash
cp .env.example .env
nano .env
```

Set these values (the same ones you had on Render):

```
DRY_RUN=true                          # start in dry-run, flip to false after one clean cycle
REQUIRE_CONFIRM=false                 # no stdin when running as a service
POLYMARKET_PRIVATE_KEY=YOUR_KEY_HERE  # hex, no 0x
POLYMARKET_FUNDER_ADDRESS=            # leave blank for MetaMask
POLYMARKET_SIGNATURE_TYPE=0           # 0 = MetaMask/EOA
TELEGRAM_BOT_TOKEN=YOUR_TOKEN
TELEGRAM_CHAT_ID=YOUR_CHAT_ID
```

Save with `Ctrl+O`, `Enter`, `Ctrl+X`.

Lock down the file so only `bot` can read the key:

```bash
chmod 600 .env
```

---

## 5. Test-run once manually

```bash
source .venv/bin/activate
python bot.py
```

You should see:
- `Polymarket near-certainty bot — DRY-RUN` banner
- `Pulled 21,xxx unique markets from Gamma`
- A scan result with 6–8 qualifying candidates
- `Simulated: N/N orders` (because DRY_RUN is still true)

Telegram should ping with a startup message + simulated orders.

Press `Ctrl+C` to stop.

---

## 6. Run it as a systemd service (always-on, auto-restart on reboot)

Create the service file:

```bash
sudo nano /etc/systemd/system/polymarket-bot.service
```

Paste:

```ini
[Unit]
Description=Polymarket near-certainty bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bot
WorkingDirectory=/home/bot/polymarket-bot
EnvironmentFile=/home/bot/polymarket-bot/.env
ExecStart=/home/bot/polymarket-bot/.venv/bin/python /home/bot/polymarket-bot/bot.py
Restart=always
RestartSec=10
StandardOutput=append:/home/bot/polymarket-bot/logs/bot.log
StandardError=append:/home/bot/polymarket-bot/logs/bot.log

[Install]
WantedBy=multi-user.target
```

Save, then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
sudo systemctl start polymarket-bot
sudo systemctl status polymarket-bot    # should show "active (running)"
```

Tail the logs:

```bash
tail -f ~/polymarket-bot/logs/bot.log
```

---

## 7. Go live

Once you've watched one clean cycle in DRY-RUN, edit `.env`:

```bash
nano ~/polymarket-bot/.env
# change DRY_RUN=true → DRY_RUN=false
```

Restart the service:

```bash
sudo systemctl restart polymarket-bot
```

Next scan (~60s) should:
- Log `LIVE TRADING` banner
- Return `POST /order "HTTP/2 200 OK"` (not 403)
- Ping Telegram with real order IDs

---

## 8. Pushing future updates

When I push new code to the GitHub repo:

```bash
cd ~/polymarket-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt   # only if requirements changed
sudo systemctl restart polymarket-bot
```

---

## 9. Useful commands cheat sheet

```bash
# Service status
sudo systemctl status polymarket-bot

# Live logs (systemd journal)
sudo journalctl -u polymarket-bot -f

# Live logs (file)
tail -f ~/polymarket-bot/logs/bot.log

# Restart
sudo systemctl restart polymarket-bot

# Stop
sudo systemctl stop polymarket-bot

# Edit env vars (requires restart after)
nano ~/polymarket-bot/.env && sudo systemctl restart polymarket-bot
```

---

## 10. Cost comparison vs. Render

| | Render Starter | Hetzner CX22 Helsinki |
|---|---|---|
| Monthly cost | $7 | €3.79 (~$4) |
| Geoblocked by Polymarket | **yes** | **no** |
| Auto-deploys from GitHub | yes | no (one `git pull`) |
| Ops burden | zero | ~1 min per update |

Worth the small ops trade to actually be able to trade.
