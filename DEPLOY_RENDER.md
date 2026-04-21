# Deploying to Render (Background Worker, $7/mo)

Total time: ~10 minutes. You'll never touch a server.

## Prerequisites

- A GitHub account (free)
- A Render account (free to sign up, billed $7/mo for the worker)
- A **dedicated Polymarket wallet** seeded with the bankroll you're willing to risk — not your main wallet
- Your private key + funder address for that wallet

## Step 1 — Push the repo to GitHub (one time, 2 min)

1. Go to https://github.com/new, create a **private** repo called `polymarket-bot`.
2. In a terminal on your laptop:
   ```bash
   cd polymarket-bot
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/polymarket-bot.git
   git push -u origin main
   ```
   The `.gitignore` already excludes `.env` and `logs/` so nothing secret ships.

## Step 2 — Deploy via Render Blueprint (3 min)

1. Go to https://dashboard.render.com/select-repo?type=blueprint
2. Connect your GitHub and pick `polymarket-bot`.
3. Render reads `render.yaml` and pre-fills everything. Click **Apply**.
4. Wait ~2 min for the first build. It will START IN DRY-RUN — no orders placed yet.

## Step 3 — Add your secrets (2 min)

In the Render dashboard → your `polymarket-bot` worker → **Environment** tab:

1. Edit `POLYMARKET_PRIVATE_KEY` → paste your key (hex, with or without `0x`).
2. Edit `POLYMARKET_FUNDER_ADDRESS` → paste your Polymarket proxy/deposit address.
   - If you use the Polymarket web UI with email login: find this in the deposit screen under "Polygon Address".
   - If you use MetaMask / EOA: same as the address derived from your key; you can leave this blank and set `POLYMARKET_SIGNATURE_TYPE=0`.
3. Click **Save, rebuild, and deploy**.

## Step 4 — Watch a dry run (2 min)

Open the **Logs** tab. Within a minute you'll see:

```
Polymarket near-certainty bot — DRY-RUN (simulated)
Top qualifying candidates: 8
  edge=+0.080 Yes bid=0.910 ask=0.910 ...
Sizing → 8 orders, total $432.18 of $1,000 bankroll
Simulated: 8/8 orders
```

Verify the picks look reasonable. Let it run a few cycles.

## Step 5 — Flip to live

In **Environment** tab, change:
```
DRY_RUN = false
```
Save & redeploy. Next scan places real orders. You're done.

## Day-to-day operation

- **Adjust strategy**: change env vars in the Render dashboard (e.g. raise `BANKROLL_USD`, tighten `MIN_EDGE`). Each save triggers a fast redeploy.
- **Pause trading**: set `DRY_RUN=true`. Takes ~30s to take effect.
- **Kill switch**: in Render, click **Suspend** on the service. Instantly stops all activity. Resume any time.
- **View fills / audit trail**: Logs tab shows every order. Structured JSONL also written to `logs/orders-YYYYMMDD.jsonl` inside the container (ephemeral — if you want permanent audit, add a Render Persistent Disk or ship logs to S3).

## Cost & resource notes

- **$7/mo** for the Starter Worker (512 MB RAM, 0.5 CPU). Bot uses ~80 MB and <1% CPU, so 6× headroom.
- Polygon gas is paid from the same wallet. Each fill costs a fraction of a cent in MATIC; seed the wallet with ~$1 of MATIC alongside your USDC.
- Polymarket maker/taker fees are currently 0.

## Updating the bot

```bash
# make your changes locally
git add . && git commit -m "tweak thresholds" && git push
```
Render auto-redeploys on every push to `main` (controlled by `autoDeploy: true` in `render.yaml`).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `POLYMARKET_PRIVATE_KEY is not set` | Secret not saved in Render dashboard. |
| `signature type 1 requires funder` | Set `POLYMARKET_FUNDER_ADDRESS` or switch to `POLYMARKET_SIGNATURE_TYPE=0` for EOA. |
| `insufficient allowance` | EOA wallet: set USDC/CTF approvals once via the Polymarket UI. Email wallet: not needed. |
| `not enough balance` | Fund the wallet with USDC.e on Polygon + a tiny bit of MATIC for gas. |
| Service keeps restarting | Check Logs tab. Usually a bad env var; the bot crash-backs-off automatically. |
