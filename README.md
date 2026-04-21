# Polymarket near-certainty bot

A Python bot that hunts Polymarket for outcomes priced near 100¢ on the
"will-definitely-happen" side, sizes them with fractional Kelly, and places
GTC limit BUYs at the best ask through the official CLOB.

## Strategy in one paragraph

Pull every active Polymarket market from the Gamma API, drop anything with
< $50k volume / < $5k liquidity, then for each YES and NO token pull the live
order book. If the **best ask ≤ 0.95** and the implied **edge =
(1 − haircut) − ask ≥ 4¢**, it's a candidate. Rank by edge, take the top 20,
size each with quarter-Kelly (capped at $200/market and 20% of best-ask depth),
and post limit BUYs at the ask so they fill on touch.

Edge definition (per your spec): **resolution edge only** — we assume true
probability ≈ 0.99 (1¢ haircut for resolution / oracle / tail risk) and only
fire when the market price gives us at least 4¢ of cushion.

## Files

| File           | Purpose                                                     |
|----------------|-------------------------------------------------------------|
| `config.py`    | All thresholds, loaded from `.env`                          |
| `scanner.py`   | Pulls Gamma markets + CLOB books → filters → `Candidate`s   |
| `sizing.py`    | Fractional-Kelly portfolio across qualifying candidates     |
| `executor.py`  | Places GTC limit BUYs (or simulates in DRY_RUN)             |
| `bot.py`       | CLI entry point — `--once` or continuous loop               |
| `.env.example` | Template for your local `.env`                              |
| `logs/`        | Daily logs, JSONL audit trail, persistent state             |

## Setup

```bash
cd polymarket-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — at minimum set BANKROLL_USD; leave DRY_RUN=true for first run
```

## First run (always dry)

```bash
python bot.py --once --dry-run
```

You'll see the top 10 candidates and the orders the bot **would** have placed.
Inspect `logs/orders-YYYYMMDD.jsonl` afterwards.

## Going live

1. Fund a Polymarket account with USDC.e on Polygon (deposit via the web UI).
2. Get your private key + funder address (see comments in `.env.example`).
3. Set token allowances **once** if using EOA/MetaMask — the Polymarket docs
   list the three contracts that need USDC + Conditional-Token approvals.
   Email/Magic-link wallets skip this step.
4. Edit `.env`:
   ```
   DRY_RUN=false
   REQUIRE_CONFIRM=true   # keep this on at first
   POLYMARKET_PRIVATE_KEY=...
   POLYMARKET_FUNDER_ADDRESS=...
   POLYMARKET_SIGNATURE_TYPE=1
   ```
5. Run `python bot.py --once`. The bot will print each proposed order and
   ask for `y/N` confirmation per fill.
6. Once you're comfortable, set `REQUIRE_CONFIRM=false` and drop `--once` to
   run a continuous scan loop.

## Knobs you'll tune most

| Env var             | Default | What it controls                                        |
|---------------------|---------|---------------------------------------------------------|
| `MAX_ASK`           | 0.95    | Hard ceiling on price; raise for fewer/safer bets       |
| `MIN_EDGE`          | 0.04    | Min cushion after haircut; raise to be pickier          |
| `HAIRCUT`           | 0.01    | Tail-risk discount on "true" 100% probability           |
| `BANKROLL_USD`      | 1000    | Total capital the bot is allowed to deploy per scan     |
| `KELLY_FRACTION`    | 0.25    | Quarter-Kelly. Drop to 0.10 for very conservative       |
| `MAX_POSITIONS`     | 20      | Diversification cap                                     |
| `MAX_PER_MARKET_USD`| 200     | Hard cap on any single bet                              |
| `MAX_PCT_OF_BOOK`   | 0.20    | Don't move the book — eat ≤20% of best-ask depth        |
| `POLL_SECONDS`      | 60      | Scan cadence in continuous mode                         |

## Things to know

- **Resolution risk is real.** "Near certainty" markets have lost — usually on
  oracle disputes or weird wording. The 1¢ haircut + Kelly fraction protects
  you against a small base rate of these. Don't run with `KELLY_FRACTION=1.0`.
- **Idempotency.** The bot won't re-bet on a market it already has a position
  in this session (state in `logs/state.json`). Delete that file to reset.
- **Ask fills, not mid.** We post at the best ask so taker orders fill on
  touch. The price can move between scan and post — if your order rests, it'll
  cancel automatically as a GTC only when the book improves past it; you may
  want to add a `cancel_after` cleanup pass for unfilled orders.
- **Fees & gas.** USDC.e settlement on Polygon. Polymarket charges no maker/
  taker fee currently, but check their docs before scaling.
- **Neg-risk markets.** The bot detects `negRisk=true` markets and they trade
  through the same flow. EOA wallets need separate allowances for the neg-risk
  exchange + adapter contracts (see Polymarket docs).

## Extending

- **Cross-side arbs (YES + NO < 1.00):** add a check in `scanner.py` after
  both tokens of a market are priced — if `ask_yes + ask_no < 0.99`, both
  legs are a guaranteed win. Easy ~30 LOC addition.
- **Position monitoring:** poll `data-api.polymarket.com` for your address and
  emit a daily P&L report.
- **Telegram/Discord alerts:** wire into the `execute()` return value.
