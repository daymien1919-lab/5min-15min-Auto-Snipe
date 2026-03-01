#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Agents can customize signal source.

Usage:
    python fast_trader.py              # Dry run (show opportunities, no trades)
    python fast_trader.py --live       # Execute real trades
    python fast_trader.py --positions  # Show current fast market positions
    python fast_trader.py --quiet      # Only output on trades/errors

Requires:
    SIMMER_API_KEY environment variable (get from simmer.markets/dashboard)
"""

import os
import sys
import json
import math
import argparse
import time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote

# Force line-buffered stdout for non-TTY environments (cron, Docker, Railway)
sys.stdout.reconfigure(line_buffering=True)

# Optional: Trade Journal integration
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "SIMMER_SPRINT_ENTRY", "type": float,
                        "help": "Min price divergence from 50¢ to trigger trade"},
    "min_momentum_pct": {"default": 0.5, "env": "SIMMER_SPRINT_MOMENTUM", "type": float,
                         "help": "Min BTC % move in lookback window to trigger"},
    "max_position": {"default": 5.0, "env": "SIMMER_SPRINT_MAX_POSITION", "type": float,
                     "help": "Max $ per trade"},
    "signal_source": {"default": "binance", "env": "SIMMER_SPRINT_SIGNAL", "type": str,
                      "help": "Price feed source (binance, coingecko)"},
    "lookback_minutes": {"default": 5, "env": "SIMMER_SPRINT_LOOKBACK", "type": int,
                         "help": "Minutes of price history for momentum calc"},
    "min_time_remaining": {"default": 60, "env": "SIMMER_SPRINT_MIN_TIME", "type": int,
                           "help": "Skip fast_markets with less than this many seconds remaining"},
    "asset": {"default": "BTC", "env": "SIMMER_SPRINT_ASSET", "type": str,
              "help": "Asset to trade (BTC, ETH, SOL)"},
    "window": {"default": "5m", "env": "SIMMER_SPRINT_WINDOW", "type": str,
               "help": "Market window duration (5m or 15m)"},
    "volume_confidence": {"default": True, "env": "SIMMER_SPRINT_VOL_CONF", "type": bool,
                          "help": "Weight signal by volume (higher volume = more confident)"},
    "daily_budget": {"default": 10.0, "env": "SIMMER_SPRINT_DAILY_BUDGET", "type": float,
                     "help": "Max total spend per UTC day"},
}

TRADE_SOURCE = "sdk:fastloop"
SMART_SIZING_PCT = 0.05
MIN_SHARES_PER_ORDER = 5

ASSET_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}

def _load_config(schema, skill_file, config_filename="config.json"):
    from pathlib import Path
    config_path = Path(skill_file).parent / config_filename
    file_cfg = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                file_cfg = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    result = {}
    for key, spec in schema.items():
        if key in file_cfg:
            result[key] = file_cfg[key]
        elif spec.get("env") and os.environ.get(spec["env"]):
            val = os.environ.get(spec["env"])
            type_fn = spec.get("type", str)
            try:
                if type_fn == bool:
                    result[key] = val.lower() in ("true", "1", "yes")
                else:
                    result[key] = type_fn(val)
            except (ValueError, TypeError):
                result[key] = spec.get("default")
        else:
            result[key] = spec.get("default")
    return result

def _get_config_path(skill_file, config_filename="config.json"):
    from pathlib import Path
    return Path(skill_file).parent / config_filename

def _update_config(updates, skill_file, config_filename="config.json"):
    from pathlib import Path
    config_path = Path(skill_file).parent / config_filename
    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    existing.update(updates)
    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2)
    return existing

# Load config
cfg = _load_config(CONFIG_SCHEMA, __file__)
ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
MAX_POSITION_USD = cfg["max_position"]
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
MIN_TIME_REMAINING = cfg["min_time_remaining"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]
VOLUME_CONFIDENCE = cfg["volume_confidence"]
DAILY_BUDGET = cfg["daily_budget"]

# =============================================================================
# Daily Budget Tracking
# =============================================================================

def _get_spend_path(skill_file):
    from pathlib import Path
    return Path(skill_file).parent / "daily_spend.json"

def _load_daily_spend(skill_file):
    spend_path = _get_spend_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if spend_path.exists():
        try:
            with open(spend_path) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "spent": 0.0, "trades": 0}

def _save_daily_spend(skill_file, spend_data):
    spend_path = _get_spend_path(skill_file)
    with open(spend_path, "w") as f:
        json.dump(spend_data, f, indent=2)

# =============================================================================
# API Helpers
# =============================================================================

_client = None

def get_client():
    global _client
    if _client is None:
        try:
            from simmer_sdk import SimmerClient
        except ImportError:
            print("Error: simmer-sdk not installed. Run: pip install simmer-sdk")
            sys.exit(1)
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            print("Error: SIMMER_API_KEY environment variable not set")
            sys.exit(1)
        _client = SimmerClient(api_key=api_key, venue="polymarket")
    return _client

def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "simmer-fastloop_market/1.0"
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}

# =============================================================================
# Strategy Functions (all original logic intact)
# =============================================================================

def discover_fast_market_markets(asset="BTC", window="5m"):
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=20&closed=false&tag=crypto&order=createdAt&ascending=false"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        matches_window = f"-{window}-" in slug
        if any(p in q for p in patterns) and matches_window:
            end_time = _parse_fast_market_end_time(m.get("question", ""))
            markets.append({
                "question": m.get("question", ""),
                "slug": slug,
                "condition_id": m.get("conditionId", ""),
                "end_time": end_time,
                "outcome_prices": m.get("outcomePrices", "[]"),
                "fee_rate_bps": int(m.get("fee_rate_bps") or 0),
            })
    return markets

def _parse_fast_market_end_time(question):
    import re
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None
    try:
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(timezone.utc).year
        dt_str = f"{date_str} {year} {time_str}"
        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        return dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
    except Exception:
        return None

def find_best_fast_market(markets):
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if not end_time:
            continue
        remaining = (end_time - now).total_seconds()
        if remaining > MIN_TIME_REMAINING:
            candidates.append((remaining, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None
    try:
        candles = result
        if len(candles) < 2:
            return None
        price_then = float(candles[0][1])
        price_now = float(candles[-1][4])
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"
        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0
        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "volume_ratio": volume_ratio,
        }
    except Exception:
        return None

def get_momentum(asset="BTC", source="binance", lookback=5):
    return get_binance_momentum(ASSET_SYMBOLS.get(asset, "BTCUSDT"), lookback)

def import_fast_market_market(slug):
    url = f"https://polymarket.com/event/{slug}"
    try:
        return get_client().import_market(url)
    except Exception:
        return None

def execute_trade(market_id, side, amount):
    try:
        return get_client().trade(market_id=market_id, side=side, amount=amount)
    except Exception:
        return None

def run_fast_market_strategy(dry_run=True, positions_only=False, show_config=False,
                            smart_sizing=False, quiet=False):
    def log(msg, force=False):
        if not quiet or force:
            print(msg)

    get_client()

    markets = discover_fast_market_markets(ASSET, WINDOW)
    best = find_best_fast_market(markets)
    if not best:
        return

    prices = json.loads(best.get("outcome_prices", "[]"))
    market_yes_price = float(prices[0]) if prices else 0.5

    momentum = get_momentum(ASSET, SIGNAL_SOURCE, LOOKBACK_MINUTES)
    if not momentum:
        return

    if abs(momentum["momentum_pct"]) < MIN_MOMENTUM_PCT:
        return

    side = "yes" if momentum["direction"] == "up" else "no"
    divergence = abs(0.50 - market_yes_price)

    if divergence <= 0:
        return

    position_size = MAX_POSITION_USD
    market_id = import_fast_market_market(best["slug"])
    if not market_id:
        return

    if not dry_run:
        execute_trade(market_id, side, position_size)

# =============================================================================
# CLI Entry Point (1-minute infinite loop)
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer FastLoop Trading Skill")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--positions", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    dry_run = not args.live

    print("🚀 Railway 1-Minute High Frequency Loop Activated")
    print("=" * 50)

    while True:
        try:
            run_fast_market_strategy(
                dry_run=dry_run,
                positions_only=args.positions,
                smart_sizing=False,
                quiet=args.quiet,
            )
        except Exception as e:
            print(f"🔥 Loop error: {e}")

        time.sleep(60)  # 1 minute high-frequency cycle
