import os
import json
from datetime import datetime, timedelta
from flask import Flask, jsonify
from flask_cors import CORS
import gspread
import yfinance as yf
import pandas as pd

app = Flask(__name__)
CORS(app)

SHEET_ID   = os.environ.get("SHEET_ID", "1RwIupOHnln5if-hzCE-bQPfT_TW7N1_sTZcPDMelb5g")
CREDS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")

# ─────────────────────────────────────────────────────────────────────────────
# Currency detection from yf ticker suffix
# ─────────────────────────────────────────────────────────────────────────────
def get_currency(yf_ticker):
    t = yf_ticker.upper()
    if t.endswith(".L"):   return "GBP"
    if t.endswith(".AS"):  return "EUR"
    if t.endswith(".PA"):  return "EUR"
    if t.endswith(".DE"):  return "EUR"
    if t.endswith(".IR"):  return "EUR"
    return "USD"

# ─────────────────────────────────────────────────────────────────────────────
# Pence detection — robust version using yfinance currency field.
#
# Background: yfinance returns LSE (.L) tickers in EITHER pence (GBx) or
# pounds (GBP) depending on the instrument. ETFs like AGGG, IGLN, WSML are
# priced in GBP on yfinance even though they trade on LSE, while others like
# INFR are returned in pence. The only reliable way to tell them apart is to
# check the ticker's reported currency via yfinance .info.
#
# "GBp" → pence  → divide price by 100 ✓
# "GBP" → pounds → use price as-is    ✓
#
# Results are cached in _pence_cache so the yfinance API is only hit once per
# ticker per process lifetime (i.e. once per Render deploy, not once per
# dashboard load).
# ─────────────────────────────────────────────────────────────────────────────
_pence_cache: dict = {}

import logging
def is_pence(yf_ticker: str) -> bool:
    """
    Returns True if the ticker is quoted in pence (GBp) according to yfinance .info['currency'].
    Logs a warning if currency cannot be determined.
    """
    t = yf_ticker.upper()
    if not t.endswith(".L"):
        return False
    if t in _pence_cache:
        return _pence_cache[t]
    try:
        info = yf.Ticker(yf_ticker).info
        currency = info.get("currency", None)
        if currency == "GBp":
            _pence_cache[t] = True
            return True
        elif currency == "GBP":
            _pence_cache[t] = False
            return False
        else:
            logging.warning(f"Ambiguous currency for {yf_ticker}: {currency}. Assuming GBP (no pence conversion).")
            _pence_cache[t] = False
            return False
    except Exception as e:
        logging.warning(f"Could not determine currency for {yf_ticker}: {e}. Assuming GBP (no pence conversion).")
        _pence_cache[t] = False
        return False

# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────────────────────────────────────
def get_sheet_data():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(creds_json)
            tmp_path = f.name
        gc = gspread.service_account(filename=tmp_path)
    else:
        gc = gspread.service_account(filename=CREDS_FILE)
    sh     = gc.open_by_key(SHEET_ID)
    trades = sh.worksheet("trades").get_all_records()
    config = sh.worksheet("portfolio_config").get_all_records()
    try:
        manual = sh.worksheet("manual_prices").get_all_records()
    except:
        manual = []
    return trades, config, manual

def parse_config(config_rows):
    cfg = {r["key"]: r["value"] for r in config_rows}
    return {
        "starting_capital": float(cfg.get("starting_capital", 100000)),
        "base_currency":    cfg.get("base_currency", "USD"),
        "inception_date":   cfg.get("inception_date", "2026-01-14"),
        "benchmark":        cfg.get("benchmark", "SPY"),
        "portfolio_name":   cfg.get("portfolio_name", "Investment Portfolio"),
    }

# ─────────────────────────────────────────────────────────────────────────────
# FX rates
# ─────────────────────────────────────────────────────────────────────────────
def get_fx_rates():
    rates = {"USD": 1.0}
    for pair, key in [("GBPUSD=X", "GBP"), ("EURUSD=X", "EUR")]:
        try:
            h = yf.Ticker(pair).history(period="2d")
            if not h.empty:
                rates[key] = float(h["Close"].iloc[-1])
        except:
            rates[key] = 1.0
    return rates

# ─────────────────────────────────────────────────────────────────────────────
# Live price fetch
# ─────────────────────────────────────────────────────────────────────────────
def get_live_price(yf_ticker, manual_map):
    if yf_ticker in manual_map:
        return float(manual_map[yf_ticker])
    try:
        h = yf.Ticker(yf_ticker).history(period="2d")
        if not h.empty:
            price = float(h["Close"].iloc[-1])
            if is_pence(yf_ticker):
                return price / 100
            return price
    except:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Position builder
# ─────────────────────────────────────────────────────────────────────────────
def build_positions(trades, fx_rates, manual_map):
    """Reconstruct open positions and closed trades from trade log."""
    from collections import defaultdict
    ticker_trades = defaultdict(list)
    for t in trades:
        if t.get("ticker") and t.get("action"):
            ticker_trades[t["ticker"]].append(t)

    open_positions = []
    closed_trades  = []

    for ticker, events in ticker_trades.items():
        events_sorted = sorted(events, key=lambda x: x["date"])
        qty_held   = 0.0
        cost_basis = 0.0
        open_date  = None
        yf_ticker  = events_sorted[0].get("yf_ticker", ticker)
        asset_class = events_sorted[0].get("asset_class", "")
        name       = events_sorted[0].get("name", ticker)
        direction  = events_sorted[0].get("direction", "LONG")
        currency_override = events_sorted[0].get('currency_override', '').strip().upper()
       
        for e in events_sorted:
            action = e.get("action", "").upper()
            qty    = float(e.get("quantity", 0))
            price  = float(e.get("price", 0))

            if action == "OPEN":
                qty_held    = qty
                cost_basis  = price * qty
                open_date   = e.get("date")
                yf_ticker   = e.get("yf_ticker", yf_ticker)
                asset_class = e.get("asset_class", asset_class)
                name        = e.get("name", name)

            elif action == "ADD":
                cost_basis += price * qty
                qty_held   += qty

            elif action == "REDUCE":
                avg      = cost_basis / qty_held if qty_held else price
                realised = (price - avg) * qty
                cost_basis -= avg * qty
                qty_held   -= qty
                closed_trades.append({
                    "ticker": ticker, "name": name,
                    "qty": qty, "entry_price": avg,
                    "exit_price": price, "realised_pnl": realised,
                    "date": e.get("date"), "yf_ticker": yf_ticker,
                    "asset_class": asset_class,
                })

            elif action == "CLOSE":
                avg      = cost_basis / qty_held if qty_held else price
                realised = (price - avg) * qty_held

                currency = currency_override if currency_override else get_currency(yf_ticker)
                pence = is_pence(yf_ticker) if not currency_override else False
                fx = fx_rates.get(currency, 1.0)

                if pence:
                    avg   /= 100
                    price /= 100
                realised_usd = realised * fx

                closed_trades.append({
                    "ticker": ticker, "name": name,
                    "qty": qty_held, "entry_price": round(avg, 4),
                    "exit_price": round(price, 4),
                    "realised_pnl_usd": round(realised_usd, 2),
                    "date": e.get("date"), "yf_ticker": yf_ticker,
                    "asset_class": asset_class,
                })
                qty_held   = 0.0
                cost_basis = 0.0

        if qty_held > 0:
            live_price = get_live_price(yf_ticker, manual_map)
            currency = currency_override if currency_override else get_currency(yf_ticker)
            pence = is_pence(yf_ticker) if not currency_override else False
            fx = fx_rates.get(currency, 1.0)

            avg_price = cost_basis / qty_held

            if live_price is not None:
                lp = live_price  # already in pounds if pence, due to get_live_price
                ap = avg_price   # also in pounds, do NOT divide by 100
                mv_local = lp * qty_held
                cost_usd = ap * qty_held * fx
                mv_usd = mv_local * fx
                unreal_pnl = mv_usd - cost_usd
                unreal_pct = (lp - ap) / ap if ap else 0
            else:
                lp = avg_price  # already in pounds if pence
                ap = lp
                mv_usd = ap * qty_held * fx
                cost_usd = mv_usd
                unreal_pnl = 0
                unreal_pct = 0

            import logging
            logging.warning(
                f"DEBUG: {ticker} | qty={qty_held} | avg_price={avg_price} | live_price={lp} | pence={pence} | fx={fx} | mv_usd={mv_usd} | cost_usd={cost_usd} | currency={currency}"
            )
            open_positions.append({
                "ticker":      ticker,
                "name":        name,
                "asset_class": asset_class,
                "direction":   direction,
                "quantity":    qty_held,
                "avg_price":   round(avg_price, 4),
                "live_price":  round(lp, 4) if live_price else None,
                "currency":    currency,
                "mv_usd":      round(mv_usd, 2),
                "cost_usd":    round(cost_usd, 2),
                "unreal_pnl":  round(unreal_pnl, 2),
                "unreal_pct":  round(unreal_pct * 100, 2),
                "open_date":   open_date,
                "yf_ticker":   yf_ticker,
            })

    return open_positions, closed_trades

# ─────────────────────────────────────────────────────────────────────────────
# NAV curve builder
# ─────────────────────────────────────────────────────────────────────────────
def build_nav_curve(trades, fx_rates, cfg, benchmark_ticker):
    """Reconstruct daily NAV from inception using yfinance historical prices."""
    inception = datetime.strptime(cfg["inception_date"], "%Y-%m-%d")
    today     = datetime.today()
    starting  = cfg["starting_capital"]

    ticker_map = {}
    for t in trades:
        if t.get("yf_ticker") and t.get("ticker"):
            ticker_map[t["ticker"]] = t.get("yf_ticker")

    all_tickers = list(set(ticker_map.values())) + ["GBPUSD=X", "EURUSD=X", benchmark_ticker]
    raw = yf.download(all_tickers,
                      start=inception.strftime("%Y-%m-%d"),
                      end=(today + timedelta(days=1)).strftime("%Y-%m-%d"),
                      auto_adjust=True, progress=False)

    if isinstance(raw.columns, pd.MultiIndex):
        prices = raw["Close"]
    else:
        prices = raw[["Close"]] if "Close" in raw.columns else raw

    prices = prices.ffill().bfill()

    from collections import defaultdict
    events_by_date = defaultdict(list)
    for t in trades:
        events_by_date[t["date"]].append(t)

    holdings    = {}
    cash        = starting
    nav_series  = []
    bench_series = []
    bench_start = None

    date_range = pd.bdate_range(start=inception, end=today)

    for dt in date_range:
        ds = dt.strftime("%Y-%m-%d")

        for e in events_by_date.get(ds, []):
            tk     = e["ticker"]
            qty    = float(e.get("quantity", 0))
            price  = float(e.get("price", 0))
            action = e.get("action", "").upper()
            ytk    = e.get("yf_ticker", tk)
            currency_override = e.get('currency_override', '').strip().upper()
            currency = currency_override if currency_override else get_currency(ytk)
            pence    = is_pence(ytk) if not currency_override else False
            fx_r     = fx_rates.get(currency, 1.0)
            price_adj = price / 100 if pence else price
            p_usd  = price_adj * fx_r

            if action == "OPEN":
               holdings[tk] = {"qty": qty, "yf_ticker": ytk, "currency_override": currency_override}
               cash -= p_usd * qty
            elif action == "ADD":
               if tk in holdings:
                  holdings[tk]["qty"] += qty
               else:
                  holdings[tk] = {"qty": qty, "yf_ticker": ytk, "currency_override": currency_override}
               cash -= p_usd * qty
            elif action in ("REDUCE", "CLOSE"):
                if tk in holdings:
                    close_qty = qty if action == "REDUCE" else holdings[tk]["qty"]
                    cash += p_usd * close_qty
                    if action == "CLOSE":
                        del holdings[tk]
                    else:
                        holdings[tk]["qty"] -= close_qty

        port_val = cash
        for tk, h in holdings.items():
            ytk               = h["yf_ticker"]
            currency_override = h.get("currency_override", "").strip().upper()
            currency          = currency_override if currency_override else get_currency(ytk)
            pence             = is_pence(ytk) if not currency_override else False
            fx_r              = fx_rates.get(currency, 1.0)
            try:
                col = ytk
                if col in prices.columns:
                    p     = float(prices.loc[:dt, col].iloc[-1])
                    p_adj = p / 100 if pence else p
                    p_usd = p_adj * fx_r
                    port_val += p_usd * h["qty"]
                else:
                    port_val += h["qty"]
            except:
                pass

        nav_series.append({"date": ds, "value": round(port_val, 2)})

        try:
            if benchmark_ticker in prices.columns:
                bp = float(prices.loc[:dt, benchmark_ticker].iloc[-1])
                if bench_start is None:
                    bench_start = bp
                bench_val = starting * (bp / bench_start)
                bench_series.append({"date": ds, "value": round(bench_val, 2)})
        except:
            pass

    return nav_series, bench_series

# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────
def calc_metrics(nav_series, starting_capital):
    if len(nav_series) < 2:
        return {}
    values        = [x["value"] for x in nav_series]
    daily_returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]

    import math
    mean_r   = sum(daily_returns) / len(daily_returns)
    variance = sum((r - mean_r)**2 for r in daily_returns) / len(daily_returns)
    std_r    = math.sqrt(variance)
    sharpe   = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0

    peak   = values[0]
    max_dd = 0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    total_return = (values[-1] - starting_capital) / starting_capital * 100

    return {
        "total_return_pct": round(total_return, 2),
        "sharpe_ratio":     round(sharpe, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "current_value":    round(values[-1], 2),
        "total_pnl":        round(values[-1] - starting_capital, 2),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/portfolio")
def portfolio():
    try:
        trades, config_rows, manual_rows = get_sheet_data()
        cfg        = parse_config(config_rows)
        fx_rates   = get_fx_rates()
        manual_map = {r["ticker"]: r["manual_price"] for r in manual_rows if r.get("ticker")}

        open_pos, closed = build_positions(trades, fx_rates, manual_map)

        total_mv   = sum(p["mv_usd"]   for p in open_pos)
        total_cost = sum(p["cost_usd"] for p in open_pos)
        cash       = cfg["starting_capital"] - total_cost
        total_val  = total_mv + max(cash, 0)

        for p in open_pos:
            p["weight_pct"] = round(p["mv_usd"] / total_val * 100, 2) if total_val else 0

        alloc = {}
        for p in open_pos:
            ac = p["asset_class"]
            alloc[ac] = round(alloc.get(ac, 0) + p["mv_usd"] / total_val * 100, 2)

        nav_series, bench_series = build_nav_curve(trades, fx_rates, cfg, cfg["benchmark"])
        metrics = calc_metrics(nav_series, cfg["starting_capital"])

        realised_total = sum(t.get("realised_pnl_usd", 0) for t in closed)

        return jsonify({
            "portfolio_name":   cfg["portfolio_name"],
            "inception_date":   cfg["inception_date"],
            "benchmark":        cfg["benchmark"],
            "starting_capital": cfg["starting_capital"],
            "current_value":    round(total_val, 2),
            "total_pnl":        round(total_mv - total_cost + realised_total, 2),
            "cash":             round(max(cash, 0), 2),
            "metrics":          metrics,
            "open_positions":   open_pos,
            "closed_trades":    closed,
            "allocation":       alloc,
            "nav_series":       nav_series,
            "benchmark_series": bench_series,
            "fx_rates":         fx_rates,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

from flask import send_from_directory

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    app.run(debug=True, port=5000)
