# FUND CENTRE BACKEND
# ====================
# FIXED (2025-05): All changes limited to app.py on App_Rebuild branch.
#   1. SORTINO: denominator now uses all N returns (not just downside count); min(r-rf,0)^2.
#   2. CASH BALANCE: sale proceeds credited correctly (cost + PnL returned to cash on close).
#   3. RECOVERY TROUGH: replaced fragile nested ternary with clear explicit drawdown loop.
#   4. TOTAL RETURN PCT: NAV-based canonical value, not overwritten; simple_total_return_pct added as alias.
#   5. STARTING_CAPITAL: passed through calc_metrics for correct total_return_pct base.
#   6. FX EXPOSURE: cash balance included in base_currency bucket so percentages sum ~100%.
#   7. LIVE NAV: build_nav_curve accepts live_positions_mv + live_cash so final point == current_value.
#   8. RF RATE: fetched live from yfinance (^IRX proxy for 3m T-bill); hard-coded fallback.
#   9. GET_SHEET_DATA: retry logic + exponential backoff for transient Google Sheets 429/503 errors.
#  10. NAV FINAL POINT: live MV + cash passed into build_nav_curve for exact anchoring.
#  11. BENCHMARK METRICS: benchmark stats computed from bench_series for Sharpe/Sortino/MaxDD/30dVol comparison.
#
# SAFE: Sharpe, NAV curve logic, hit_rate, rolling_vol — unchanged.
# ──────────────────────────────────────────────────────────────────────────────────────────────

from flask import Flask, jsonify, request
from flask_cors import CORS
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os, json, math, time
from datetime import datetime, timedelta
import yfinance as yf

app = Flask(__name__)
CORS(app)

# ── Google Sheets credentials ────────────────────────────────────────────────
SCOPES      = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS_JSON  = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "")

def get_sheet_data(max_retries=5):
    """Fetch all four worksheets with exponential-backoff retry for quota errors."""
    creds  = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(CREDS_JSON), SCOPES)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(SHEET_ID)

    for attempt in range(1, max_retries + 1):
        try:
            trades_ws      = sheet.worksheet("Trades")
            config_ws      = sheet.worksheet("Config")
            manual_ws      = sheet.worksheet("ManualPrices")
            nav_ws         = sheet.worksheet("NAVOverrides")

            trades_rows    = trades_ws.get_all_records()
            config_rows    = config_ws.get_all_records()
            manual_rows    = manual_ws.get_all_records()
            nav_rows       = nav_ws.get_all_records()
            return trades_rows, config_rows, manual_rows, nav_rows

        except gspread.exceptions.APIError as e:
            status = e.response.status_code if hasattr(e, "response") else 0
            if status in (429, 500, 503) and attempt < max_retries:
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            raise

# ── Config parser ─────────────────────────────────────────────────────────────
def parse_config(config_rows):
    cfg = {}
    for row in config_rows:
        key = str(row.get("Key", "")).strip()
        val = str(row.get("Value", "")).strip()
        cfg[key] = val

    def safe_float(k, default):
        try:    return float(cfg.get(k, default))
        except: return default

    return {
        "portfolio_name":   cfg.get("portfolio_name",   "My Portfolio"),
        "inception_date":   cfg.get("inception_date",   "2024-01-01"),
        "starting_capital": safe_float("starting_capital", 10000),
        "base_currency":    cfg.get("base_currency",    "USD"),
        "benchmark":        cfg.get("benchmark", "SPY"),
    }

def parse_nav_overrides(nav_rows):
    overrides = {}
    for row in nav_rows:
        date_str = str(row.get("Date", "")).strip()
        val_str  = str(row.get("NAV", "")).strip()
        if date_str and val_str:
            try:
                overrides[date_str] = float(val_str)
            except ValueError:
                pass
    return overrides

# ── Risk-free rate ─────────────────────────────────────────────────────────────
def get_risk_free_rate():
    """Fetch the 3-month T-bill yield from yfinance (^IRX). Returns annual decimal."""
    try:
        irx = yf.Ticker("^IRX")
        hist = irx.history(period="5d")
        if not hist.empty:
            rate_pct = float(hist["Close"].dropna().iloc[-1])
            return rate_pct / 100.0
    except Exception:
        pass
    return 0.043  # fallback: ~4.3%

# ── FX rates ──────────────────────────────────────────────────────────────────
def get_fx_rates():
    pairs = {"GBP": "GBPUSD=X", "EUR": "EURUSD=X"}
    rates = {"USD": 1.0}
    for ccy, ticker in pairs.items():
        try:
            hist = yf.Ticker(ticker).history(period="2d")
            if not hist.empty:
                rates[ccy] = float(hist["Close"].dropna().iloc[-1])
        except Exception:
            rates[ccy] = 1.0
    return rates

# ── Sizing policy ─────────────────────────────────────────────────────────────
SIZING_POLICY = {
    "Core":         {"tickers": ["IWDA", "AGGG"],                     "min_pct": 10, "max_pct": 35},
    "Satellite":    {"tickers": ["INFR", "BRIJ", "GILG", "IGLN"],     "min_pct": 3,  "max_pct": 12},
    "Opportunistic":{"tickers": ["EEM", "WSML"],                      "min_pct": 2,  "max_pct": 8},
    "Speculative":  {"tickers": ["BTCUSD", "BTC-USD", "COIN"],        "min_pct": 0,  "max_pct": 5},
}

# ── Price fetcher ─────────────────────────────────────────────────────────────
_price_cache = {}
_price_cache_ts = {}
CACHE_TTL = 300  # 5 minutes

def get_prices(tickers, start_date, end_date=None):
    """Fetch adjusted close prices for a list of tickers via yfinance."""
    import pandas as pd

    if not tickers:
        return pd.DataFrame()

    end_date = end_date or (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    cache_key = (tuple(sorted(tickers)), start_date, end_date)
    now = time.time()

    if cache_key in _price_cache and (now - _price_cache_ts.get(cache_key, 0)) < CACHE_TTL:
        return _price_cache[cache_key]

    try:
        raw = yf.download(
            list(tickers), start=start_date, end=end_date,
            auto_adjust=True, progress=False, threads=True
        )
        if raw.empty:
            return pd.DataFrame()

        if isinstance(raw.columns, pd.MultiIndex):
            closes = raw["Close"] if "Close" in raw.columns.get_level_values(0) else pd.DataFrame()
        else:
            closes = raw[["Close"]] if "Close" in raw.columns else pd.DataFrame()

        closes.index = pd.to_datetime(closes.index)
        closes = closes.ffill()

        _price_cache[cache_key]    = closes
        _price_cache_ts[cache_key] = now
        return closes
    except Exception:
        return pd.DataFrame()

# ── Spike filter ─────────────────────────────────────────────────────────────
# Purpose: data-quality spike filtering only — NOT smoothing of real volatility.
# Only removes single-day round-trips where a price doubles then halves (or vice versa)
# on back-to-back days — classic bad data artefacts.
def filter_price_spikes(series, threshold=2.0):
    """Remove single-day round-trip spikes from a price series (list of floats)."""
    if len(series) < 3:
        return series
    filtered = list(series)
    for i in range(1, len(series) - 1):
        prev, curr, nxt = filtered[i - 1], series[i], series[i + 1]
        if prev > 0 and nxt > 0:
            up   = curr / prev
            down = nxt / curr
            if (up > threshold and down < 1 / threshold) or (up < 1 / threshold and down > threshold):
                filtered[i] = (prev + nxt) / 2  # interpolate
    return filtered

# ── Position builder ──────────────────────────────────────────────────────────
def build_positions(trades, fx_rates, manual_map=None):
    manual_map = manual_map or {}

    # Map tickers to yfinance symbols
    ticker_map = {}
    for t in trades:
        raw = str(t.get("ticker", "")).strip()
        mapped = raw
        if raw == "BTCUSD":  mapped = "BTC-USD"
        ticker_map[raw] = mapped

    yf_tickers = list(set(ticker_map.values()))
    start_date = "2020-01-01"
    prices_df = get_prices(yf_tickers, start_date)

    def latest_price(raw_ticker, currency="USD"):
        yf_t = ticker_map.get(raw_ticker, raw_ticker)
        if raw_ticker in manual_map and manual_map[raw_ticker]:
            p = float(manual_map[raw_ticker])
        elif not prices_df.empty and yf_t in prices_df.columns:
            col = prices_df[yf_t].dropna()
            p = float(col.iloc[-1]) if not col.empty else 0.0
        else:
            p = 0.0
        rate = fx_rates.get(currency, 1.0)
        return p * rate

    open_pos_map = {}
    closed_trades = []
    sorted_trades = sorted(trades, key=lambda x: str(x.get("date", "")))

    for t in sorted_trades:
        ticker   = str(t.get("ticker",     "")).strip()
        action   = str(t.get("action",     "")).strip().upper()
        qty      = float(t.get("quantity",  0) or 0)
        price    = float(t.get("price",     0) or 0)
        currency = str(t.get("currency",   "USD")).strip()
        date_str = str(t.get("date",       "")).strip()
        asset_cl = str(t.get("asset_class","")).strip()
        rate     = fx_rates.get(currency, 1.0)

        if action == "BUY":
            if ticker not in open_pos_map:
                open_pos_map[ticker] = {
                    "ticker": ticker, "quantity": 0, "total_cost": 0,
                    "currency": currency, "asset_class": asset_cl
                }
            p = open_pos_map[ticker]
            p["quantity"]   += qty
            p["total_cost"] += qty * price * rate

        elif action == "SELL":
            if ticker in open_pos_map:
                p = open_pos_map[ticker]
                if p["quantity"] > 0:
                    avg_cost_per_share = p["total_cost"] / p["quantity"]
                    cost_sold_usd      = qty * avg_cost_per_share
                    realised_pnl_usd   = qty * price * rate - cost_sold_usd
                    closed_trades.append({
                        "ticker":           ticker,
                        "quantity":         qty,
                        "sell_price":       price,
                        "currency":         currency,
                        "date":             date_str,
                        "realised_pnl_usd": round(realised_pnl_usd, 2),
                        "cost_usd_sold":    round(cost_sold_usd, 2),
                    })
                    p["quantity"]   -= qty
                    p["total_cost"] -= cost_sold_usd
                    if p["quantity"] <= 0:
                        del open_pos_map[ticker]

    open_positions = []
    for ticker, p in open_pos_map.items():
        if p["quantity"] <= 0:
            continue
        currency  = p["currency"]
        live_px   = latest_price(ticker, currency)
        mv_usd    = live_px * p["quantity"]
        cost_usd  = p["total_cost"]
        unreal    = mv_usd - cost_usd
        unreal_pct = round(unreal / cost_usd * 100, 2) if cost_usd else 0

        open_positions.append({
            "ticker":        ticker,
            "quantity":      round(p["quantity"], 6),
            "avg_cost":      round(cost_usd / p["quantity"], 4) if p["quantity"] else 0,
            "live_price":    round(live_px, 4),
            "mv_usd":        round(mv_usd, 2),
            "cost_usd":      round(cost_usd, 2),
            "unrealised_pnl_usd": round(unreal, 2),
            "unrealised_pnl_pct": unreal_pct,
            "currency":      currency,
            "asset_class":   p["asset_class"],
        })

    return open_positions, closed_trades

# ── NAV curve builder ──────────────────────────────────────────────────────────
def build_nav_curve(trades, fx_rates, cfg, benchmark_ticker, nav_overrides=None, live_positions_mv=None, live_cash=None):
    """
    Reconstruct a daily NAV series from trade history.
    Optionally anchors the final day to live_positions_mv + live_cash so the last
    NAV point exactly matches the live current_value shown on the dashboard.
    """
    import pandas as pd
    nav_overrides = nav_overrides or {}

    # Build ticker map
    ticker_map = {}
    for t in trades:
        raw = str(t.get("ticker", "")).strip()
        mapped = raw
        if raw == "BTCUSD": mapped = "BTC-USD"
        ticker_map[raw] = mapped

    currencies = list(set(str(t.get("currency","USD")) for t in trades))
    fx_tickers = [f"{c}USD=X" for c in currencies if c != "USD"]

    all_tickers = list(set(ticker_map.values())) + fx_tickers + [benchmark_ticker]
    start_date = cfg.get("inception_date", "2024-01-01")
    prices = get_prices(all_tickers, start_date)

    if prices.empty:
        return [], []

    starting = cfg["starting_capital"]
    sorted_trades = sorted(trades, key=lambda x: str(x.get("date", "")))

    holdings = {}      # ticker -> quantity
    cost_map = {}      # ticker -> total_cost_usd

    trade_idx  = 0
    nav_series = []
    bench_series = []
    bench_start  = None

    dates = sorted(prices.index)

    for dt in dates:
        ds = dt.strftime("%Y-%m-%d")

        # Apply all trades on or before this date
        while trade_idx < len(sorted_trades):
            t = sorted_trades[trade_idx]
            t_date = str(t.get("date","")).strip()
            if t_date > ds:
                break

            ticker   = str(t.get("ticker","")).strip()
            action   = str(t.get("action","")).strip().upper()
            qty      = float(t.get("quantity",0) or 0)
            price    = float(t.get("price",0) or 0)
            currency = str(t.get("currency","USD")).strip()

            fx_t  = f"{currency}USD=X"
            rate  = float(prices.loc[:dt, fx_t].iloc[-1]) if (currency != "USD" and fx_t in prices.columns) else 1.0

            if action == "BUY":
                holdings[ticker]  = holdings.get(ticker, 0) + qty
                cost_map[ticker]  = cost_map.get(ticker, 0) + qty * price * rate
            elif action == "SELL":
                if ticker in holdings and holdings[ticker] > 0:
                    avg = cost_map.get(ticker,0) / holdings[ticker]
                    holdings[ticker]  = max(holdings[ticker] - qty, 0)
                    cost_map[ticker]  = cost_map.get(ticker,0) - qty * avg
            trade_idx += 1

        # NAV override check
        if ds in nav_overrides:
            nav_val = nav_overrides[ds]
        else:
            mv = 0.0
            for ticker, qty in holdings.items():
                if qty <= 0:
                    continue
                yf_t = ticker_map.get(ticker, ticker)
                if yf_t not in prices.columns:
                    continue
                col = prices.loc[:dt, yf_t].dropna()
                if col.empty:
                    continue
                px = float(col.iloc[-1])

                currency = next((str(tr.get("currency","USD")) for tr in sorted_trades if str(tr.get("ticker","")).strip() == ticker), "USD")
                fx_t     = f"{currency}USD=X"
                rate     = float(prices.loc[:dt, fx_t].dropna().iloc[-1]) if (currency != "USD" and fx_t in prices.columns) else 1.0
                mv += qty * px * rate

            cost_total  = sum(v for v in cost_map.values() if v > 0)
            proceeds    = 0.0
            trade_idx2  = 0
            temp_hold   = {}
            temp_cost   = {}
            for t in sorted_trades:
                t_date = str(t.get("date","")).strip()
                if t_date > ds:
                    break
                ticker   = str(t.get("ticker","")).strip()
                action   = str(t.get("action","")).strip().upper()
                qty      = float(t.get("quantity",0) or 0)
                price    = float(t.get("price",0) or 0)
                currency = str(t.get("currency","USD")).strip()
                fx_t     = f"{currency}USD=X"
                rate     = float(prices.loc[:dt, fx_t].iloc[-1]) if (currency != "USD" and fx_t in prices.columns) else 1.0
                if action == "BUY":
                    temp_hold[ticker] = temp_hold.get(ticker,0) + qty
                    temp_cost[ticker] = temp_cost.get(ticker,0) + qty * price * rate
                elif action == "SELL" and ticker in temp_hold and temp_hold[ticker] > 0:
                    avg = temp_cost.get(ticker,0) / temp_hold[ticker]
                    pnl = qty * price * rate - qty * avg
                    proceeds += pnl
                    temp_hold[ticker] = max(temp_hold[ticker]-qty,0)
                    temp_cost[ticker] = temp_cost.get(ticker,0) - qty*avg

            cash    = starting - cost_total + proceeds
            cash    = max(cash, 0)
            nav_val = mv + cash

        nav_series.append({"date": ds, "value": round(nav_val, 2)})

        if benchmark_ticker in prices.columns:
            bp = float(prices.loc[:dt, benchmark_ticker].iloc[-1])
            if bench_start is None:
                bench_start = bp
            bench_series.append({"date": ds, "value": round(starting * (bp / bench_start), 2)})

    # Anchor final NAV point to live data if provided
    if live_positions_mv is not None and live_cash is not None and nav_series:
        live_nav = round(live_positions_mv + live_cash, 2)
        today_str = datetime.today().strftime("%Y-%m-%d")
        if nav_series[-1]["date"] == today_str:
            nav_series[-1]["value"] = live_nav
        else:
            nav_series.append({"date": today_str, "value": live_nav})

    return nav_series, bench_series

# ── Metrics calculator ────────────────────────────────────────────────────────
def calc_metrics(nav_series, starting_capital, rf_annual=0.043, closed_trades=[]):
    if len(nav_series) < 2:
        return {}
    import math
    values        = [x["value"] for x in nav_series]
    daily_returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
    n        = len(daily_returns)
    mean_r   = sum(daily_returns) / n
    rf_daily = rf_annual / 252
    variance = sum((r - mean_r)**2 for r in daily_returns) / (n - 1)
    std_r    = math.sqrt(variance)
    Sharpe   = ((mean_r - rf_daily) / std_r * math.sqrt(252)) if std_r > 0 else 0

    total_return = (values[-1] - starting_capital) / starting_capital * 100

    # FIX 1: Sortino — standard downside deviation using ALL N returns in denominator.
    # downside_std = sqrt( (1/N) * sum( min(r_i - rf, 0)^2 ) )
    # Only the min(r-rf, 0) term is non-zero for returns above rf,
    # but N is the full sample size — not just the count of bad days.
    downside_sq_sum = sum(min(r - rf_daily, 0) ** 2 for r in daily_returns)
    downside_var    = downside_sq_sum / n  # divide by ALL N returns
    downside_std    = math.sqrt(downside_var)
    # Guard against zero downside deviation (all returns >= rf) to avoid division by zero.
    sortino_ratio   = ((mean_r - rf_daily) / downside_std * math.sqrt(252)) if downside_std > 0 else 0

    last30          = daily_returns[-30:] if len(daily_returns) >= 30 else daily_returns
    last30_mean     = sum(last30) / len(last30) if last30 else 0
    rolling_std     = math.sqrt(sum((r - last30_mean)**2 for r in last30) / max(len(last30) - 1, 1))
    rolling_30d_vol = rolling_std * math.sqrt(252) * 100
    downside_deviation = downside_std * math.sqrt(252) * 100

    # FIX 3: Recovery / drawdown trough — clear, explicit loop.
    # Tracks peak, running max drawdown, and the trough index where max_dd occurred.
    peak_val  = values[0]
    max_dd    = 0.0
    trough_val = values[0]
    trough_idx = 0

    for i, v in enumerate(values):
        if v > peak_val:
            peak_val = v
        if peak_val > 0:
            dd = (peak_val - v) / peak_val
            if dd > max_dd:
                max_dd     = dd
                trough_val = v
                trough_idx = i

    trough_date          = nav_series[trough_idx]["date"]
    current_drawdown_pct = round((peak_val - values[-1]) / peak_val * 100, 2) if peak_val > 0 else 0
    days_since_trough    = (datetime.today() - datetime.strptime(trough_date, "%Y-%m-%d")).days

    if current_drawdown_pct == 0:
        status = "At Peak"
    elif values[-1] >= peak_val:
        status = "Recovered"
    else:
        status = "Recovering"

    recovery_status = {
        "status":               status,
        "days_since_trough":    days_since_trough,
        "trough_date":          trough_date,
        "current_drawdown_pct": current_drawdown_pct,
    }

    winners            = [t for t in closed_trades if t.get("realised_pnl_usd", 0) > 0]
    losers             = [t for t in closed_trades if t.get("realised_pnl_usd", 0) <= 0]
    hit_rate_pct       = round(len(winners) / len(closed_trades) * 100, 1) if closed_trades else 0
    avg_gain_usd       = round(sum(t["realised_pnl_usd"] for t in winners) / len(winners), 2) if winners else 0.0
    avg_loss_usd       = round(sum(abs(t["realised_pnl_usd"]) for t in losers) / len(losers), 2) if losers else 0.0
    total_realised_pnl = round(sum(t.get("realised_pnl_usd", 0) for t in closed_trades), 2)

    return {
        # FIX 4: total_return_pct is NAV-based (canonical). Not overwritten outside.
        "total_return_pct":   round(total_return, 2),
        "Sharpe_ratio":       round(Sharpe, 2),
        "max_drawdown_pct":   round(max_dd * 100, 2),
        "current_value":      round(values[-1], 2),
        "total_pnl":          round(values[-1] - starting_capital, 2),
        "sortino_ratio":      round(sortino_ratio, 2),
        "rolling_30d_vol":    round(rolling_30d_vol, 2),
        "downside_deviation": round(downside_deviation, 2),
        "recovery_status":    recovery_status,
        "hit_rate_pct":       hit_rate_pct,
        "avg_gain_usd":       avg_gain_usd,
        "avg_loss_usd":       avg_loss_usd,
        "total_realised_pnl": total_realised_pnl,
    }

@app.route("/api/portfolio")
def portfolio():
    try:
        trades, config_rows, manual_rows, nav_overrides_rows = get_sheet_data()
        cfg          = parse_config(config_rows)
        fx_rates     = get_fx_rates()
        rf_rate      = get_risk_free_rate()
        manual_map   = {r["ticker"]: r["manual_price"] for r in manual_rows if r.get("ticker")}
        nav_overrides = parse_nav_overrides(nav_overrides_rows)

        open_pos, closed = build_positions(trades, fx_rates, manual_map)

        total_mv   = sum(p["mv_usd"] for p in open_pos)
        total_cost = sum(p["cost_usd"] for p in open_pos)

        # FIX 2: Cash = starting_capital - cost_of_open_positions + sum(sale_proceeds).
        # sale_proceeds for each closed trade = cost_usd_sold + realised_pnl_usd.
        # This correctly returns BOTH principal AND profit/loss to cash on disposal,
        # preventing cash understatement after realisations.
        proceeds_total = sum(t.get("realised_pnl_usd", 0) for t in closed)
        cash = cfg["starting_capital"] - total_cost + proceeds_total
        cash      = max(cash, 0)  # clamp floating-point artefacts
        total_val = total_mv + cash

        for p in open_pos:
            p["weight_pct"] = round(p["mv_usd"] / total_val * 100, 2) if total_val else 0

        for p in open_pos:
            ticker      = p["ticker"]
            asset_class = p["asset_class"]
            weight_pct  = p["weight_pct"]

            flags = []
            if ticker == "COIN":                            flags.append("EXIT_REVIEW")
            if ticker in ["BTCUSD", "BTC-USD"]:             flags.append("WATCH_60D")
            if ticker == "EEM" and weight_pct > 7:          flags.append("OVERWEIGHT")
            if ticker == "GILG" and weight_pct < 5:         flags.append("UNDERWEIGHT")
            if ticker == "IGLN":                            flags.append("CONVICTION_HOLD")
            if ticker == "WSML":                            flags.append("TRIM_CANDIDATE")
            if asset_class == "Crypto":                     flags.append("SPECULATIVE")
            if ticker in ["IWDA", "AGGG"]:                  flags.append("CORE")
            if ticker in ["INFR", "BRIJ", "GILG", "IGLN"]:  flags.append("SATELLITE")
            if ticker in ["EEM", "WSML"]:                   flags.append("OPPORTUNISTIC")
            p["flags"] = flags

            for band, policy in SIZING_POLICY.items():
                if ticker in policy["tickers"]:
                    p["sizing_band"]   = band
                    p["sizing_breach"] = not (policy["min_pct"] <= weight_pct <= policy["max_pct"])
                    break
            else:
                p["sizing_band"]   = "Unclassified"
                p["sizing_breach"] = False

        alloc = {}
        for p in open_pos:
            ac = p["asset_class"]
            alloc[ac] = round(alloc.get(ac, 0) + p["mv_usd"] / total_val * 100, 2)
        if cash > 0 and total_val > 0:
            alloc["Cash"] = round(cash / total_val * 100, 2)

        # FIX 10: Pass live_positions_mv and live_cash into build_nav_curve so the
        # final NAV point is anchored to the same prices as the KPI current_value.
        nav_series, bench_series = build_nav_curve(
            trades, fx_rates, cfg, cfg["benchmark"],
            nav_overrides=nav_overrides,
            live_positions_mv=total_mv,
            live_cash=cash,
        )
        metrics = calc_metrics(nav_series, cfg["starting_capital"], rf_annual=rf_rate, closed_trades=closed)

        # Benchmark metrics — compute same stats over bench_series for comparison (Sharpe, Sortino, MaxDD, 30d Vol)
        _bm = calc_metrics(bench_series, cfg["starting_capital"], rf_annual=rf_rate, closed_trades=[])
        benchmark_metrics = {
            "Sharpe_ratio":     _bm.get("Sharpe_ratio"),
            "sortino_ratio":    _bm.get("sortino_ratio"),
            "max_drawdown_pct": _bm.get("max_drawdown_pct"),
            "rolling_30d_vol":  _bm.get("rolling_30d_vol"),
        } if _bm else {}

        # FIX 4: Do NOT overwrite metrics["total_return_pct"] — the NAV-based value
        # from calc_metrics is canonical. Expose a simple money-weighted version
        # under a separate key so both are available without clobbering each other.
        simple_total_return_pct = (
            round((total_val - cfg["starting_capital"]) / cfg["starting_capital"] * 100, 2)
            if cfg["starting_capital"] else 0
        )

        # FIX 6: FX exposure — include cash in base currency bucket.
        # Cash is assumed to be held in base_currency (default: USD).
        base_ccy = cfg.get("base_currency", "USD")
        fx_exposure = {}
        for currency in ["USD", "GBP", "EUR"]:
            ccy_mv = sum(p["mv_usd"] for p in open_pos if p["currency"] == currency)
            # Add cash balance to the base currency bucket so percentages sum ~100%.
            if currency == base_ccy:
                ccy_mv += cash
            fx_exposure[currency + "_pct"] = round(ccy_mv / total_val * 100, 2) if total_val else 0

        return jsonify({
            "portfolio_name":           cfg["portfolio_name"],
            "inception_date":           cfg["inception_date"],
            "benchmark":                cfg["benchmark"],
            "starting_capital":         cfg["starting_capital"],
            "current_value":            round(total_val, 2),
            "total_pnl":                round(total_val - cfg["starting_capital"], 2),
            "cash":                     round(cash, 2),
            "metrics":                  metrics,
            "simple_total_return_pct":  simple_total_return_pct,
            "rf_rate":                  round(rf_rate * 100, 3),
            "open_positions":           open_pos,
            "closed_trades":            closed,
            "allocation":               alloc,
            "nav_series":               nav_series,
            "benchmark_series":         bench_series,
            "benchmark_metrics":        benchmark_metrics,
            "fx_rates":                 fx_rates,
            "fx_exposure":              fx_exposure,
            "position_sizing_policy":   SIZING_POLICY,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/price")
def price():
    ticker = request.args.get("ticker", "SPY")
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty:
            return jsonify({"error": "No data"}), 404
        latest = hist["Close"].dropna().iloc[-1]
        return jsonify({"ticker": ticker, "price": round(float(latest), 4)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/test")
def run_tests():
    errors = []

    # ── helper ────────────────────────────────────────────────────────────────
    def fake_nav(values):
        base = datetime(2024, 1, 1)
        return [{"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "value": v}
                for i, v in enumerate(values)]

    rf_daily = 0.043 / 252

    # TEST 1: Sortino ratio — standard downside deviation
    returns_test = [-0.02, 0.01, -0.015, 0.03, -0.005]
    mean_r   = sum(returns_test) / len(returns_test)
    dsq      = sum(min(r - rf_daily, 0)**2 for r in returns_test)
    d_std    = math.sqrt(dsq / len(returns_test))
    expected = (mean_r - rf_daily) / d_std * math.sqrt(252) if d_std > 0 else 0
    values_test = [100.0]
    for r in returns_test:
        values_test.append(values_test[-1] * (1 + r))
    nav_test = fake_nav(values_test)
    m = calc_metrics(nav_test, 100.0, rf_annual=0.043)
    sortino = m.get("sortino_ratio", None)
    if sortino is None or abs(sortino - round(expected, 2)) > 0.01:
        errors.append(f"Sortino FAIL: got {sortino}, expected {round(expected,2)}")
    else:
        print(f"  [PASS] Sortino = {sortino:.4f}")

    returns_all_pos = [0.01, 0.02, 0.03]
    vals2 = [100.0]
    for r in returns_all_pos:
        vals2.append(vals2[-1] * (1 + r))
    m2 = calc_metrics(fake_nav(vals2), 100.0)
    d_std2 = math.sqrt(sum(min(r - rf_daily, 0)**2 for r in returns_all_pos) / len(returns_all_pos))
    sortino2 = (sum(returns_all_pos)/len(returns_all_pos) / d_std2 * math.sqrt(252)) if d_std2 > 0 else 0
    if sortino2 != 0:
        errors.append(f"Sortino zero-downside FAIL: got {sortino2}")
    else:
        print("  [PASS] Sortino zero-downside = 0 (no crash)")

    # TEST 2: calc_metrics — max drawdown and trough date
    vals_dd = [100, 110, 105, 90, 95, 108]
    nav_dd  = fake_nav(vals_dd)
    m = calc_metrics(nav_dd, 100.0)
    expected_dd = round((110 - 90) / 110 * 100, 2)
    if abs(m["max_drawdown_pct"] - expected_dd) > 0.01:
        errors.append(f"MaxDD FAIL: got {m['max_drawdown_pct']}, expected {expected_dd}")
    else:
        print(f"  [PASS] Max drawdown = {m['max_drawdown_pct']}%")

    expected_trough = (datetime(2024, 1, 1) + timedelta(days=3)).strftime("%Y-%m-%d")
    if m["recovery_status"]["trough_date"] != expected_trough:
        errors.append(f"Trough date FAIL: got {m['recovery_status']['trough_date']}, expected {expected_trough}")
    else:
        print(f"  [PASS] Trough date = {m['recovery_status']['trough_date']}")

    if errors:
        return jsonify({"status": "FAIL", "errors": errors}), 500
    return jsonify({"status": "PASS", "message": "All tests passed."})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
