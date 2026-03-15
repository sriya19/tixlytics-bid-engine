#!/usr/bin/env python3
"""
Tixlytics Bid Engine
====================

A selective, concentrated bid engine for secondary ticket markets.

Design principles:
  1. Event-level gating — weak events get steep haircuts and risk premiums.
  2. Resale estimates use ATP-anchored blending with multiplicative haircuts.
  3. Positions are capped by market capacity (what the section can plausibly
     absorb before the event), not just portfolio share.
  4. Capital is deployed only when opportunities clear quality bars.
     Undeployed cash is an explicit, acceptable outcome.
  5. Robust parsing and defensive defaults so the script works on any
     events file with the same schema, even if the events change.

Usage:
    python bid_engine.py events.json
"""

import json
import re
import sys
from dataclasses import dataclass, field
from statistics import median
from typing import Optional


# =============================================================================
# CONFIGURATION
# =============================================================================

TOTAL_CAPITAL = 50_000.00
RESALE_FEE = 0.10
SAFETY_MARGIN = 0.84
MIN_MARGIN_PCT = 0.15
MAX_RISK = 6
MAX_POSITION_PCT = 0.20
MIN_POSITION_DOLLARS = 1000.00
MIN_TURNOVER = 0.03
MIN_DAILY_VOL = 3.0
MIN_BID_FLOOR = 10.00
OUTBID_INCREMENT = 5.00
DEFAULT_SPLIT = 2

# Market capacity: don't assume we can exit more than this much inventory
# before the event.
HOLDING_FACTOR = 0.50
HOLDING_CAP_DAYS = 5.0

# Resale estimate weights
W_SEC_ATP = 0.70
W_LOW_ASK = 0.20
W_EVT_ATP = 0.10

# Event classification thresholds
STRONG_DAILY_VOL = 40
WEAK_DAILY_VOL = 10
STRONG_DAYS_OUT = 10
WEAK_DAYS_OUT = 21
WEAK_PRIMARY_AVG = 20.0

SEP = "═" * 76
THIN = "─" * 76


# =============================================================================
# SAFE HELPERS
# =============================================================================

def safe_float(value, default=0.0):
    """Best-effort float conversion with fallback."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    """Best-effort int conversion with fallback."""
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_split(split):
    """Ensure split size is always a positive integer."""
    s = safe_int(split, DEFAULT_SPLIT)
    return s if s > 0 else DEFAULT_SPLIT


# =============================================================================
# SECTION NORMALIZATION
# =============================================================================

_ALIASES = [
    (r"\bfront\s*mezz(?:anine)?\b", "FRONT MEZZANINE"),
    (r"\brear\s*mezz(?:anine)?\b", "REAR MEZZANINE"),
    (r"\bmezz(?:anine)?\b", "MEZZANINE"),
    (r"\bfl(?:oor)?\b", "FLOOR"),
    (r"\borch(?:estra)?\b", "ORCHESTRA"),
    (r"\bsec(?:tion)?\b", "SECTION"),
]


def normalize_section(raw) -> str:
    """Normalize section names across sources; safe for None/non-strings."""
    if raw is None:
        return ""
    name = str(raw).strip().upper()
    if not name:
        return ""
    for pat, repl in _ALIASES:
        name = re.sub(pat, repl, name, flags=re.IGNORECASE)
    name = re.sub(r"[-_]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class EventProfile:
    event_id: str
    name: str
    venue: str
    date: str
    days_to_event: int
    event_atp: float
    event_daily_vol: float
    event_market_qty: int
    avg_primary_avail: float
    has_competitors: bool
    strength: str = ""
    strength_reasons: list = field(default_factory=list)


@dataclass
class SectionOpp:
    event_id: str
    event_name: str
    section: str

    # Listing features
    lowest_ask: float
    median_ask: float
    ask_spread: float
    num_listings: int

    # Transaction features
    section_atp: float
    event_atp: float
    atp_ask_ratio: float
    daily_volume: float
    total_qty: int
    turnover: float

    # Primary
    face_value: float
    pct_available: float

    # Competition
    competitor_bid: Optional[float]
    competitor_split: Optional[int]

    # Event context
    days_to_event: int
    event_strength: str

    # Computed fields
    expected_resale: float = 0.0
    haircut_pct: float = 0.0
    max_profitable_bid: float = 0.0
    target_bid: float = 0.0
    risk_score: int = 0
    risk_detail: str = ""
    profit_per_ticket: float = 0.0
    margin_pct: float = 0.0
    liq_score: float = 0.0
    market_capacity: float = 0.0
    capital: float = 0.0
    qty: int = 0
    action: str = "SKIP"
    action_reason: str = ""


# =============================================================================
# EVENT-LEVEL CLASSIFICATION
# =============================================================================

def classify_event(event: dict) -> EventProfile:
    """
    Classify an event as STRONG / NEUTRAL / WEAK.

    This lets the engine become more selective in structurally weak markets.
    """
    ed = (event.get("sales_data") or {}).get("event_level") or {}
    primary = event.get("primary_availability") or []
    bids = event.get("current_highest_bids") or []

    valid_primary = [
        safe_float(p.get("pct_available"))
        for p in primary
        if isinstance(p, dict)
    ]
    avg_prim = sum(valid_primary) / len(valid_primary) if valid_primary else 0.0

    profile = EventProfile(
        event_id=str(event.get("event_id", "")),
        name=str(event.get("name", "")),
        venue=str(event.get("venue", "")),
        date=str(event.get("date", "")),
        days_to_event=max(safe_int(event.get("days_to_event"), 0), 0),
        event_atp=safe_float(ed.get("atp")),
        event_daily_vol=safe_float(ed.get("daily_volume")),
        event_market_qty=max(safe_int(ed.get("total_market_qty"), 0), 0),
        avg_primary_avail=avg_prim,
        has_competitors=len(bids) > 0,
    )

    reasons = []
    bull = 0
    bear = 0

    if profile.event_daily_vol >= STRONG_DAILY_VOL:
        bull += 2
        reasons.append(f"high volume ({profile.event_daily_vol:.0f}/d)")
    elif profile.event_daily_vol >= WEAK_DAILY_VOL:
        bull += 1
        reasons.append(f"moderate volume ({profile.event_daily_vol:.0f}/d)")
    else:
        bear += 2
        reasons.append(f"low volume ({profile.event_daily_vol:.0f}/d)")

    if profile.days_to_event <= STRONG_DAYS_OUT:
        bull += 1
        reasons.append(f"imminent ({profile.days_to_event}d)")
    elif profile.days_to_event > WEAK_DAYS_OUT:
        bear += 1
        reasons.append(f"distant ({profile.days_to_event}d)")

    if avg_prim > WEAK_PRIMARY_AVG:
        bear += 2
        reasons.append(f"high primary avail ({avg_prim:.0f}%)")
    elif avg_prim > 5:
        bear += 1
        reasons.append(f"some primary avail ({avg_prim:.0f}%)")
    else:
        bull += 1
        reasons.append("sold out on primary")

    if profile.has_competitors:
        bull += 1
        reasons.append("active competitor bids")
    else:
        bear += 1
        reasons.append("no competitor bids (unvalidated)")

    net = bull - bear
    if net >= 3:
        profile.strength = "STRONG"
    elif net <= -1:
        profile.strength = "WEAK"
    else:
        profile.strength = "NEUTRAL"

    profile.strength_reasons = reasons
    return profile


# =============================================================================
# FEATURE EXTRACTION
# =============================================================================

def extract_sections(event: dict, profile: EventProfile) -> list[SectionOpp]:
    """
    Build section opportunities from the union of listings, sales, primary,
    and bids. Then skip only when required pricing inputs are missing.
    """
    sec_prices = {}
    for lst in event.get("secondary_listings") or []:
        if not isinstance(lst, dict):
            continue
        sec = normalize_section(lst.get("section"))
        price = safe_float(lst.get("price"), None)
        if not sec or price is None or price <= 0:
            continue
        sec_prices.setdefault(sec, []).append(price)

    sales_idx = {}
    for s in (event.get("sales_data") or {}).get("section_level") or []:
        if not isinstance(s, dict):
            continue
        sec = normalize_section(s.get("section"))
        if sec:
            sales_idx[sec] = s

    prim_idx = {}
    for p in event.get("primary_availability") or []:
        if not isinstance(p, dict):
            continue
        sec = normalize_section(p.get("section"))
        if sec:
            prim_idx[sec] = p

    bid_idx = {}
    for b in event.get("current_highest_bids") or []:
        if not isinstance(b, dict):
            continue
        sec = normalize_section(b.get("section"))
        if sec:
            bid_idx[sec] = b

    opps = []
    all_sections = sorted(set(sec_prices) | set(sales_idx) | set(prim_idx) | set(bid_idx))

    for sec in all_sections:
        prices = sorted(sec_prices.get(sec, []))
        sd = sales_idx.get(sec)
        pr = prim_idx.get(sec, {})
        bd = bid_idx.get(sec)

        # Required minimum inputs
        if sd is None:
            continue
        if not prices:
            continue

        lo = safe_float(prices[0], 0.0)
        med = safe_float(median(prices), lo)
        sec_atp = safe_float(sd.get("atp"))
        tq = max(safe_int(sd.get("total_qty"), 1), 1)
        dv = safe_float(sd.get("daily_volume"))

        opps.append(
            SectionOpp(
                event_id=profile.event_id,
                event_name=profile.name,
                section=sec,
                lowest_ask=lo,
                median_ask=med,
                ask_spread=(max(prices) - min(prices)) if prices else 0.0,
                num_listings=len(prices),
                section_atp=sec_atp,
                event_atp=profile.event_atp,
                atp_ask_ratio=(sec_atp / lo if lo > 0 else 0.0),
                daily_volume=dv,
                total_qty=tq,
                turnover=(dv / tq if tq > 0 else 0.0),
                face_value=safe_float(pr.get("face_value")),
                pct_available=safe_float(pr.get("pct_available")),
                competitor_bid=(safe_float(bd.get("bid_price")) if bd else None),
                competitor_split=(normalize_split(bd.get("split_qty")) if bd else None),
                days_to_event=profile.days_to_event,
                event_strength=profile.strength,
            )
        )

    return opps


# =============================================================================
# RESALE ESTIMATION
# =============================================================================

def estimate_resale(opp: SectionOpp) -> tuple[float, float]:
    """
    ATP-anchored blended estimate with multiplicative haircuts.
    """
    base = (
        W_SEC_ATP * opp.section_atp
        + W_LOW_ASK * opp.lowest_ask
        + W_EVT_ATP * opp.event_atp
    )

    mult = 1.0

    # Primary availability
    if opp.pct_available > 50:
        mult *= 0.85
    elif opp.pct_available > 20:
        mult *= 0.90
    elif opp.pct_available > 5:
        mult *= 0.95

    # Daily volume
    if opp.daily_volume < 1:
        mult *= 0.80
    elif opp.daily_volume < 3:
        mult *= 0.90
    elif opp.daily_volume < 5:
        mult *= 0.95

    # Inventory pressure
    if opp.total_qty > 300:
        mult *= 0.95
    elif opp.total_qty > 200:
        mult *= 0.97

    # Time horizon
    if opp.days_to_event > 25:
        mult *= 0.93
    elif opp.days_to_event > 14:
        mult *= 0.96

    # Weak event
    if opp.event_strength == "WEAK":
        mult *= 0.88

    mult = max(mult, 0.0)
    haircut = 1.0 - mult
    return base * mult, haircut


# =============================================================================
# RISK SCORING
# =============================================================================

def score_risk(opp: SectionOpp) -> tuple[int, str]:
    """Point-based risk score. Higher = riskier."""
    risk = 0
    tags = []

    if opp.pct_available > 50:
        risk += 3
        tags.append("prim>50%")
    elif opp.pct_available > 20:
        risk += 2
        tags.append("prim>20%")
    elif opp.pct_available > 5:
        risk += 1
        tags.append("prim>5%")

    if opp.daily_volume < 1:
        risk += 3
        tags.append("vol<1/d")
    elif opp.daily_volume < 3:
        risk += 3
        tags.append("vol<3/d")
    elif opp.daily_volume < 5:
        risk += 2
        tags.append("vol<5/d")

    if opp.total_qty > 300:
        risk += 1
        tags.append("inv>300")
    elif opp.total_qty > 200:
        risk += 1
        tags.append("inv>200")

    if opp.days_to_event > 25:
        risk += 2
        tags.append(">25d out")
    elif opp.days_to_event > 14:
        risk += 1
        tags.append(">14d out")

    if opp.event_strength == "WEAK":
        risk += 2
        tags.append("weak event")

    return risk, " | ".join(tags)


# =============================================================================
# COMPETITION HANDLING
# =============================================================================

def handle_competition(opp: SectionOpp) -> tuple[float, str]:
    """Decide final bid price relative to current highest bid."""
    if opp.competitor_bid is None:
        return opp.target_bid, "no competition"

    if opp.competitor_bid >= opp.target_bid:
        return 0.0, f"competitor ${opp.competitor_bid:,.0f} >= target ${opp.target_bid:,.0f}"

    proposed = min(opp.competitor_bid + OUTBID_INCREMENT, opp.target_bid)
    return proposed, f"outbid competitor ${opp.competitor_bid:,.0f}"


# =============================================================================
# ALLOCATION
# =============================================================================

def allocation_score(opp: SectionOpp) -> float:
    """
    Liquidity-adjusted profit velocity:
    profit_per_ticket * turnover / (risk + 1)
    """
    if opp.profit_per_ticket <= 0:
        return 0.0
    turnover = opp.daily_volume / (opp.total_qty + 1)
    return opp.profit_per_ticket * turnover / (opp.risk_score + 1)


def compute_market_capacity(opp: SectionOpp) -> float:
    """
    Maximum dollar size the section can plausibly absorb before the event.
    """
    safe_days = min(opp.days_to_event * HOLDING_FACTOR, HOLDING_CAP_DAYS)
    safe_days = max(safe_days, 0.0)
    return opp.daily_volume * safe_days * opp.target_bid


def allocate(opps: list[SectionOpp]) -> float:
    """
    Three-cap allocation:
      1. Score-proportional share of TOTAL_CAPITAL
      2. Hard cap per position
      3. Market-capacity cap

    No redistribution of unused capital. Cash remains cash.
    """
    active = [o for o in opps if o.action == "BID" and o.liq_score > 0]
    if not active:
        return 0.0

    total_score = sum(o.liq_score for o in active)
    if total_score <= 0:
        return 0.0

    hard_cap = TOTAL_CAPITAL * MAX_POSITION_PCT

    for opp in active:
        score_share = TOTAL_CAPITAL * (opp.liq_score / total_score)
        opp.market_capacity = compute_market_capacity(opp)
        opp.capital = min(score_share, hard_cap, opp.market_capacity)

    # Remove tiny positions
    for opp in active:
        if opp.capital < MIN_POSITION_DOLLARS:
            opp.action = "PASS"
            opp.action_reason = f"allocation ${opp.capital:,.0f} < min ${MIN_POSITION_DOLLARS:,.0f}"
            opp.capital = 0.0
            opp.qty = 0

    deployed = 0.0

    for opp in active:
        if opp.action != "BID" or opp.target_bid <= 0:
            continue

        split = normalize_split(opp.competitor_split)
        raw_qty = int(opp.capital / opp.target_bid)
        qty = raw_qty - (raw_qty % split)

        # Important robustness fix: never force qty larger than affordable capital
        if qty < split:
            opp.action = "PASS"
            opp.action_reason = f"allocation too small for split size {split}"
            opp.capital = 0.0
            opp.qty = 0
            continue

        opp.qty = qty
        opp.capital = qty * opp.target_bid
        deployed += opp.capital

    return deployed


# =============================================================================
# MAIN ENGINE
# =============================================================================

def run(data: dict) -> tuple[list[EventProfile], list[SectionOpp]]:
    profiles = []
    all_opps = []

    for event in data.get("events") or []:
        if not isinstance(event, dict):
            continue

        profile = classify_event(event)
        profiles.append(profile)

        sections = extract_sections(event, profile)

        for opp in sections:
            # Resale estimate
            opp.expected_resale, opp.haircut_pct = estimate_resale(opp)

            # Bid pricing
            opp.max_profitable_bid = opp.expected_resale * (1 - RESALE_FEE)
            opp.target_bid = opp.max_profitable_bid * SAFETY_MARGIN

            # Risk
            opp.risk_score, opp.risk_detail = score_risk(opp)

            # Competition
            final_bid, comp_note = handle_competition(opp)

            # Profit
            opp.profit_per_ticket = (
                opp.expected_resale * (1 - RESALE_FEE) - final_bid
            )
            opp.margin_pct = (
                opp.profit_per_ticket / final_bid if final_bid > 0 else 0.0
            )

            # Decision gates
            if opp.risk_score >= MAX_RISK:
                opp.action = "SKIP"
                opp.action_reason = f"risk {opp.risk_score} >= {MAX_RISK} ({opp.risk_detail})"
                all_opps.append(opp)
                continue

            if opp.daily_volume < MIN_DAILY_VOL:
                opp.action = "SKIP"
                opp.action_reason = f"daily volume {opp.daily_volume:.0f} < {MIN_DAILY_VOL:.0f} min"
                all_opps.append(opp)
                continue

            if opp.turnover < MIN_TURNOVER:
                opp.action = "SKIP"
                opp.action_reason = f"turnover {opp.turnover:.1%} < {MIN_TURNOVER:.0%} min"
                all_opps.append(opp)
                continue

            if final_bid <= 0:
                opp.action = "PASS"
                opp.action_reason = comp_note
                all_opps.append(opp)
                continue

            if opp.margin_pct < MIN_MARGIN_PCT:
                opp.action = "PASS"
                opp.action_reason = f"margin {opp.margin_pct:.1%} < {MIN_MARGIN_PCT:.0%} min"
                all_opps.append(opp)
                continue

            if opp.event_strength == "WEAK" and opp.risk_score > 2:
                opp.action = "SKIP"
                opp.action_reason = f"weak event + risk {opp.risk_score} — insufficient edge"
                all_opps.append(opp)
                continue

            if final_bid < MIN_BID_FLOOR:
                opp.action = "PASS"
                opp.action_reason = f"bid ${final_bid:.0f} below floor"
                all_opps.append(opp)
                continue

            opp.target_bid = final_bid
            opp.action = "BID"
            opp.action_reason = comp_note
            opp.liq_score = allocation_score(opp)
            all_opps.append(opp)

    allocate(all_opps)
    return profiles, all_opps


# =============================================================================
# OUTPUT
# =============================================================================

def print_event_summaries(profiles: list[EventProfile]):
    print(f"\n{SEP}")
    print("  A. EVENT SUMMARIES")
    print(SEP)

    for p in profiles:
        tag = {"STRONG": "🟢", "NEUTRAL": "🟡", "WEAK": "🔴"}.get(p.strength, "⚪")
        print(f"\n  {tag} {p.name}")
        print(f"     {p.venue}  •  {p.date}  •  {p.days_to_event}d out")
        print(
            f"     ATP ${p.event_atp:,.0f}  •  {p.event_daily_vol:.0f} tix/day"
            f"  •  {p.event_market_qty} listed  •  avg primary {p.avg_primary_avail:.0f}%"
        )
        print(f"     Classification: {p.strength}")
        for r in p.strength_reasons:
            print(f"       • {r}")


def print_section_table(opps: list[SectionOpp]):
    print(f"\n{SEP}")
    print("  B. SECTION OPPORTUNITY TABLE")
    print(SEP)

    by_event = {}
    for o in opps:
        by_event.setdefault(o.event_id, []).append(o)

    for eid, secs in by_event.items():
        event_name = secs[0].event_name if secs else eid
        print(f"\n  Event: {event_name} ({eid})")
        print(
            f"  {'Section':<22} {'Resale':>8} {'Haircut':>8} {'MaxBid':>8} "
            f"{'Bid':>8} {'Profit':>8} {'Turn%':>6} {'Risk':>5} {'Action'}"
        )
        print(f"  {THIN}")

        for o in sorted(secs, key=lambda x: (-x.liq_score, -x.expected_resale, x.section)):
            bid_s = f"${o.target_bid:,.0f}" if o.action == "BID" else "—"
            prf_s = f"${o.profit_per_ticket:,.0f}" if o.action == "BID" else "—"
            turn_s = f"{o.turnover*100:.1f}%"
            act_s = {"BID": "BID ✓", "PASS": "PASS ✗", "SKIP": "SKIP ✗"}.get(o.action, o.action)

            print(
                f"  {o.section:<22} "
                f"${o.expected_resale:>7,.0f} "
                f"{o.haircut_pct:>7.1%} "
                f"${o.max_profitable_bid:>7,.0f} "
                f"{bid_s:>8} "
                f"{prf_s:>8} "
                f"{turn_s:>6} "
                f"{o.risk_score:>5} "
                f"{act_s}"
            )

            if o.action != "BID":
                print(f"  {'':22} → {o.action_reason}")


def print_portfolio(opps: list[SectionOpp]):
    bids = [o for o in opps if o.action == "BID" and o.qty > 0]
    deployed = sum(o.capital for o in bids)
    exp_profit = sum(o.profit_per_ticket * o.qty for o in bids)
    undeployed = TOTAL_CAPITAL - deployed

    print(f"\n{SEP}")
    print("  C. PORTFOLIO SUMMARY")
    print(SEP)

    if bids:
        print(
            f"\n  {'Event':<18} {'Section':<22} {'Bid':>8} {'Qty':>5} {'Capital':>10} "
            f"{'ExpProfit':>10} {'Margin':>7} {'Risk':>5}"
        )
        print(f"  {THIN}")

        for o in sorted(bids, key=lambda x: (-x.capital, x.event_name, x.section)):
            ep = o.profit_per_ticket * o.qty
            print(
                f"  {o.event_name[:18]:<18} {o.section:<22} ${o.target_bid:>7,.0f} "
                f"{o.qty:>5} ${o.capital:>9,.0f} "
                f"${ep:>9,.0f} "
                f"{o.margin_pct:>6.0%} "
                f"{o.risk_score:>5}"
            )

    print(f"\n  {THIN}")
    print(f"  Capital deployed:     ${deployed:>12,.2f}")
    print(f"  Capital undeployed:   ${undeployed:>12,.2f}")
    print(f"  Expected profit:      ${exp_profit:>12,.2f}")

    if deployed > 0:
        print(f"  Expected ROI:         {exp_profit / deployed:>12.1%}")
    else:
        print("  Expected ROI:                  N/A")

    if undeployed > 500:
        print("\n  Remaining sections fail risk-adjusted margin and liquidity thresholds.")

    print(f"\n{SEP}")


def print_final_bid_decisions(opps: list[SectionOpp]):
    print(f"\n{SEP}")
    print("  FINAL BID DECISIONS")
    print(SEP)

    bids = [o for o in opps if o.action == "BID" and o.qty > 0]
    if not bids:
        print("  No bids recommended.")
        return

    for o in sorted(bids, key=lambda x: (-x.capital, x.event_id, x.section)):
        print(
            f"  EVENT {o.event_id} | {o.event_name} | SECTION {o.section} | "
            f"BID ${o.target_bid:.2f}/ticket | QTY {o.qty} | CAPITAL ${o.capital:.2f} | "
            f"WHY: margin {o.margin_pct:.0%}, turnover {o.turnover:.1%}, risk {o.risk_score}"
        )


def print_commentary(opps: list[SectionOpp], profiles: list[EventProfile]):
    print(f"\n{SEP}")
    print("  D. COMMENTARY")
    print(SEP)

    for p in profiles:
        secs = [o for o in opps if o.event_id == p.event_id]
        bids = [o for o in secs if o.action == "BID" and o.qty > 0]
        skips = [o for o in secs if o.action in ("SKIP", "PASS")]
        cap = sum(o.capital for o in bids)

        print(f"\n  {p.name}  [{p.strength}]")

        if bids:
            for b in sorted(bids, key=lambda x: -x.capital):
                print(
                    f"    ✓ {b.section}: ${b.capital:,.0f} "
                    f"({b.margin_pct:.0%} margin, {b.turnover*100:.1f}% turnover, risk {b.risk_score})"
                )

        if skips:
            for s in skips:
                print(f"    ✗ {s.section}: {s.action_reason}")

        print(f"    Capital: ${cap:,.0f}")

    print()


def print_model_notes():
    print(f"\n{SEP}")
    print("  E. MODEL NOTES")
    print(SEP)
    print(
        """
  Pricing model
    expected_resale = (0.70 x section_ATP + 0.20 x lowest_ask
                       + 0.10 x event_ATP) x haircut_multiplier

    Haircuts stack multiplicatively for: high primary availability,
    low daily volume, large inventory, long time horizon, weak event.
    This prevents stale ATP from dominating in structurally weak sections.

  Risk model
    Point-based. Skip threshold: 6.
    Steep penalties: vol<3/d, prim>50%, weak event, and long time horizon.

  Capital allocation
    Three-layer cap per position:
      1. Score-proportional share of $50k
      2. Hard cap at 20% of capital ($10k)
      3. Market capacity = daily_volume x safe_holding_days x bid_price
    No redistribution of unused capital. Cash is a position.

  Robustness features
    - safe_float / safe_int parsing for missing or messy numeric values
    - safe handling for empty arrays and missing bids
    - non-string-safe section normalization
    - union-based section discovery across listings, sales, primary, bids
    - split-size validation so quantity never exceeds affordable capital
"""
    )


# =============================================================================
# ENTRYPOINT
# =============================================================================

def main():
    if len(sys.argv) < 2:
        print("Usage: python bid_engine.py events.json")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)

    profiles, opps = run(data)

    print_event_summaries(profiles)
    print_section_table(opps)
    print_portfolio(opps)
    print_final_bid_decisions(opps)
    print_commentary(opps, profiles)
    print_model_notes()


if __name__ == "__main__":
    main()
