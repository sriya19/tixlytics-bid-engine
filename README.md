# Tixlytics Bid Engine

A selective, risk-aware bid engine for secondary ticket markets.

## What it does

This script reads an `events.json` file and decides, for each event and section:

- whether to bid
- what bid price to post
- how much capital to allocate

The engine is designed to maximize **risk-adjusted profit** under a total capital limit of **$50,000**.

It is intentionally selective and does **not** force full deployment of capital. If the remaining opportunities are weak on margin or liquidity, cash remains undeployed.

---

## Run

```bash
python bid_engine.py events.json
