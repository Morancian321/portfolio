# FUND CENTRE BACKEND — ADDITIONS
# =================================
# NEW ENDPOINTS:
#   GET /api/price_history?ticker=X&period=Y
#   GET /api/trade_rationale
#
# NEW /api/portfolio FIELDS:
#   metrics.sortino_ratio, metrics.rolling_30d_vol, metrics.downside_deviation
#   metrics.recovery_status, metrics.hit_rate_pct, metrics.avg_gain_usd
#   metrics.avg_loss_usd, metrics.total_realised_pnl
#   open_positions[].flags[], open_positions[].sizing_band,
#   open_positions[].sizing_breach
#   fx_exposure{}, position_sizing_policy{}
#
# TODO: Create 'trade_rationale' tab in Google Sheets with columns:
#   ticker | asset_class | entry_date | entry_rationale | exit_date |
#   exit_rationale | realised_pnl_usd | lessons

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

SHEET_ID = os.environ.get("SHEET_ID", "1RwIupOHnln5if-hzCE-bQPfT_TW7N1_sTZcPDMelb5g")
CREDS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")

SIZING_POLICY = {
    "Core":          {"tickers": ["IWDA", "AGGG"],                  "min_pct": 20, "max_pct": 30},
    "Satellite":     {"tickers": ["INFR", "BRIJ", "GILG", "IGLN"], "min_pct": 5,  "max_pct": 12},
    "Opportunistic": {"tickers": ["EEM", "WSML"],                   "min_pct": 3,  "max_pct": 7},
    "Speculative":   {"tickers": ["BTCUSD", "BTC-USD", "COIN"],     "min_pct": 0,  "max_pct": 2},
}

# Currency detection from yf ticker suffix
def get_currency(yf_ticker):
    t = yf_ticker.upper()
    if t.endswith(".L"):   return "GBP"
    if t.endswith(".AS"):  return "EUR"
    if t.endswith(".PA"):  return "EUR"
    if t.endswith(".DE"):  return "EUR"
    if t.endswith(".IR"):  return "EUR"
    return "USD"

def is_lse(yf_ticker):
    return yf_ticker.upper().endswith(".L")

def normalize_lse_price(raw_price, avg_price_gbp):
    """
    yfinance .L tickers inconsistently return pence or GBP depending on the stock.
    If raw_price is >50x the known GBP avg cost, it must be in pence — divide by 100.
    Otherwise it's already in GBP.
    avg_price_gbp is the sheet cost already converted to GBP (pence sheet price / 100).
    """
    if avg_price_gbp > 0 and raw_price > avg_price_gbp * 50:
        return raw_price / 100
    return raw_price

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
    sh = gc.open_by_key(SHEET_ID)
    trades   = sh.worksheet("trades").get_all_records()
    config   = sh.worksheet("portfolio_config").get_all_records()
    try:
        manual   = sh.worksheet("manual_prices").get_all_records()
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

def get_risk_free_rate():
    """Fetch annualised risk-free rate from 13-week US T-bill yield (^IRX).
    ^IRX is quoted as a percentage, e.g. 4.32 means 4.32%, so divide by 100."""
    try:
        h = yf.Ticker("^IRX").history(period="5d")
        if not h.empty:
            return float(h["Close"].iloc[-1]) / 100
    except:
        pass
    return 0.043  # fallback if yfinance unavailable

def get_live_price(yf_ticker, manual_map):
    if yf_ticker in manual_map:
        return float(manual_map[yf_ticker])
    try:
        h = yf.Ticker(yf_ticker).history(period="2d")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except:
        pass
    return None

def _is_tv_no_fx(trade_row):
    """Returns True if tv_no_fx=TRUE in the sheet.
    TradingView treats these positions as if the GBP price were in USD
    (no GBP->USD conversion). We skip FX here to match broker values."""
    val = str(trade_row.get("tv_no_fx", "")).strip().upper()
    return val in ("TRUE", "1", "YES")

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
        qty_held = 0.0
        cost_basis = 0.0
        open_date = None
        yf_ticker = events_sorted[0].get("yf_ticker", ticker)
        asset_class = events_sorted[0].get("asset_class", "")
        name = events_sorted[0].get("name", ticker)
        direction = events_sorted[0].get("direction", "LONG")
        tv_no_fx = False

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
                tv_no_fx    = _is_tv_no_fx(e)

            elif action == "ADD":
                cost_basis += price * qty
                qty_held   += qty

            elif action == "REDUCE":
                avg = cost_basis / qty_held if qty_held else price
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
                avg = cost_basis / qty_held if qty_held else price
                realised = (price - avg) * qty_held

                currency = get_currency(yf_ticker)
                fx       = fx_rates.get(currency, 1.0)
                # Sheet prices for LSE are in pence — convert to GBP for USD calc
                if is_lse(yf_ticker):
                    avg   /= 100
                    price /= 100
                realised_usd = realised / 100 * fx if is_lse(yf_ticker) else realised * fx

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
            live_price   = get_live_price(yf_ticker, manual_map)
            currency     = get_currency(yf_ticker)
            fx           = fx_rates.get(currency, 1.0)
            effective_fx = 1.0 if tv_no_fx else fx

            avg_price = cost_basis / qty_held

            # For LSE tickers, sheet prices are stored in pence — convert to GBP
            if is_lse(yf_ticker):
                ap = avg_price / 100
            else:
                ap = avg_price

            if live_price is not None:
                # Normalize live price: yfinance .L tickers inconsistently
                # return pence or GBP — use avg cost as sanity reference
                if is_lse(yf_ticker):
                    lp = normalize_lse_price(live_price, ap)
                else:
                    lp = live_price

                cost_usd   = ap * qty_held * effective_fx
                mv_usd     = lp * qty_held * effective_fx
                unreal_pnl = mv_usd - cost_usd
                unreal_pct = (lp - ap) / ap if ap else 0
            else:
                lp         = ap
                mv_usd     = ap * qty_held * effective_fx
                cost_usd   = mv_usd
                unreal_pnl = 0
                unreal_pct = 0

            open_positions.append({
                "ticker":      ticker,
                "name":        name,
                "asset_class": asset_class,
                "direction":   direction,
                "quantity":    qty_held,
                "avg_price":   round(ap, 4),
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
    raw = yf.download(all_tickers, start=inception.strftime("%Y-%m-%d"),
                      end=(today + timedelta(days=1)).strftime("%Y-%m-%d"),
                      auto_adjust=True, progress=False)

    if isinstance(raw.columns, pd.MultiIndex):
        prices = raw["Close"]
    else:
        prices = raw[["Close"]] if "Close" in raw.columns else raw

    prices = prices.ffill().bfill()
    prices.index = prices.index.tz_localize(None)

    from collections import defaultdict
    events_by_date = defaultdict(list)
    for t in trades:
        events_by_date[t["date"]].append(t)

    # Pre-compute avg costs per ticker for normalize_lse_price reference
    # We track this as we process events chronologically
    holdings = {}  # ticker -> {qty, yf_ticker, tv_no_fx, avg_cost_gbp}
    cash = starting
    nav_series = []
    bench_series = []
    bench_start = None

    date_range = pd.bdate_range(start=inception, end=today)

    for dt in date_range:
        ds = dt.strftime("%Y-%m-%d")

        for e in events_by_date.get(ds, []):
            tk       = e["ticker"]
            qty      = float(e.get("quantity", 0))
            price    = float(e.get("price", 0))
            action   = e.get("action", "").upper()
            ytk      = e.get("yf_ticker", tk)
            currency = get_currency(ytk)
            no_fx    = _is_tv_no_fx(e)
            fx_r     = fx_rates.get(currency, 1.0)

            # Sheet prices for LSE are in pence — convert to GBP for cash flow
            price_gbp = price / 100 if is_lse(ytk) else price
            p_usd     = price_gbp * (1.0 if no_fx else fx_r)

            if action == "OPEN":
                holdings[tk] = {
                    "qty": qty, "yf_ticker": ytk, "tv_no_fx": no_fx,
                    "avg_cost_gbp": price_gbp
                }
                cash -= p_usd * qty
            elif action == "ADD":
                if tk in holdings:
                    old = holdings[tk]
                    total_qty  = old["qty"] + qty
                    avg_cost   = (old["avg_cost_gbp"] * old["qty"] + price_gbp * qty) / total_qty
                    holdings[tk]["qty"] = total_qty
                    holdings[tk]["avg_cost_gbp"] = avg_cost
                else:
                    holdings[tk] = {
                        "qty": qty, "yf_ticker": ytk, "tv_no_fx": no_fx,
                        "avg_cost_gbp": price_gbp
                    }
                cash -= p_usd * qty
            elif action in ("REDUCE", "CLOSE"):
                if tk in holdings:
                    close_qty = qty if action == "REDUCE" else holdings[tk]["qty"]
                    cash += p_usd * close_qty
                    if action == "CLOSE":
                        del holdings[tk]
                    else:
                        holdings[tk]["qty"] -= close_qty

        # Portfolio value on this date
        port_val = cash
        for tk, h in holdings.items():
            ytk      = h["yf_ticker"]
            currency = get_currency(ytk)
            fx_r     = 1.0 if h.get("tv_no_fx", False) else fx_rates.get(currency, 1.0)
            avg_cost_gbp = h.get("avg_cost_gbp", 0)
            try:
                col = ytk
                if col in prices.columns:
                    p_raw = float(prices.loc[:dt, col].iloc[-1])
                    if is_lse(ytk):
                        p_gbp = normalize_lse_price(p_raw, avg_cost_gbp)
                    else:
                        p_gbp = p_raw
                    p_usd = p_gbp * fx_r
                    port_val += p_usd * h["qty"]
                else:
                    # ticker not in price data — fall back to cost basis (NAV stays flat)
                    port_val += h["avg_cost_gbp"] * h["qty"] * fx_r
            except (KeyError, IndexError):
                # price slice failed — fall back to cost basis rather than dropping position
                port_val += h["avg_cost_gbp"] * h["qty"] * fx_r

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

def calc_metrics(nav_series, starting_capital, rf_annual=0.043, closed_trades=[]):
    if len(nav_series) < 2:
        return {}
    values = [x["value"] for x in nav_series]
    daily_returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]

    import math
    n = len(daily_returns)
    mean_r = sum(daily_returns) / n
    rf_daily = rf_annual / 252
    variance = sum((r - mean_r)**2 for r in daily_returns) / (n - 1)
    std_r = math.sqrt(variance)
    Sharpe = ((mean_r - rf_daily) / std_r * math.sqrt(252)) if std_r > 0 else 0

    peak = values[0]
    max_dd = 0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    total_return = (values[-1] - starting_capital) / starting_capital * 100

    # --- Sortino Ratio ---
    downside_returns = [r for r in daily_returns if r < rf_daily]
    downside_std = math.sqrt(sum((r - rf_daily)**2 for r in downside_returns) / len(downside_returns)) if downside_returns else 0
    sortino_ratio = ((mean_r - rf_daily) / downside_std * math.sqrt(252)) if downside_std > 0 else 0

    # --- Rolling 30-day Volatility ---
    last30 = daily_returns[-30:] if len(daily_returns) >= 30 else daily_returns
    last30_mean = sum(last30) / len(last30) if last30 else 0
    rolling_std = math.sqrt(sum((r - last30_mean)**2 for r in last30) / max(len(last30) - 1, 1))
    rolling_30d_vol = rolling_std * math.sqrt(252) * 100

    # --- Downside Deviation (annualised %) ---
    downside_deviation = downside_std * math.sqrt(252) * 100

    # --- Recovery Status ---
    peak_val = values[0]
    peak_idx = 0
    trough_val = values[0]
    trough_idx = 0
    for i, v in enumerate(values):
        if v > peak_val:
            peak_val = v
            peak_idx = i
        dd = (peak_val - v) / peak_val if peak_val > 0 else 0
        if dd > (peak_val - trough_val) / peak_val if peak_val > 0 else False:
            trough_val = v
            trough_idx = i
    trough_date = nav_series[trough_idx]["date"]
    current_drawdown_pct = round((peak_val - values[-1]) / peak_val * 100, 2) if peak_val > 0 else 0
    days_since_trough = (datetime.today() - datetime.strptime(trough_date, "%Y-%m-%d")).days
    if current_drawdown_pct == 0:
        status = "At Peak"
    elif values[-1] >= peak_val:
        status = "Recovered"
    else:
        status = "Recovering"
    recovery_status = {
        "status": status,
        "days_since_trough": days_since_trough,
        "trough_date": trough_date,
        "current_drawdown_pct": current_drawdown_pct,
    }

    # --- Hit Rate, Avg Gain/Loss, Total Realised PnL (from closed_trades) ---
    winners = [t for t in closed_trades if t.get("realised_pnl_usd", 0) > 0]
    losers  = [t for t in closed_trades if t.get("realised_pnl_usd", 0) <= 0]
    hit_rate_pct = round(len(winners) / len(closed_trades) * 100, 1) if closed_trades else 0
    avg_gain_usd = round(sum(t["realised_pnl_usd"] for t in winners) / len(winners), 2) if winners else 0.0
    avg_loss_usd = round(sum(abs(t["realised_pnl_usd"]) for t in losers) / len(losers), 2) if losers else 0.0
    total_realised_pnl = round(sum(t.get("realised_pnl_usd", 0) for t in closed_trades), 2)

    return {
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
        trades, config_rows, manual_rows = get_sheet_data()
        cfg        = parse_config(config_rows)
        fx_rates   = get_fx_rates()
        rf_rate    = get_risk_free_rate()
        manual_map = {r["ticker"]: r["manual_price"] for r in manual_rows if r.get("ticker")}

        open_pos, closed = build_positions(trades, fx_rates, manual_map)

        total_mv    = sum(p["mv_usd"] for p in open_pos)
        total_cost  = sum(p["cost_usd"] for p in open_pos)
        cash        = cfg["starting_capital"] - total_cost
        total_val   = total_mv + max(cash, 0)

        for p in open_pos:
            p["weight_pct"] = round(p["mv_usd"] / total_val * 100, 2) if total_val else 0

        # --- Flags and Sizing Band (computed after weight_pct is set) ---
        for p in open_pos:
            ticker      = p["ticker"]
            asset_class = p["asset_class"]
            weight_pct  = p["weight_pct"]

            flags = []
            if ticker == "COIN":                          flags.append("EXIT_REVIEW")
            if ticker in ["BTCUSD", "BTC-USD"]:           flags.append("WATCH_60D")
            if ticker == "EEM" and weight_pct > 7:        flags.append("OVERWEIGHT")
            if ticker == "GILG" and weight_pct < 5:       flags.append("UNDERWEIGHT")
            if ticker == "IGLN":                          flags.append("CONVICTION_HOLD")
            if ticker == "WSML":                          flags.append("TRIM_CANDIDATE")
            if asset_class == "Crypto":                   flags.append("SPECULATIVE")
            if ticker in ["IWDA", "AGGG"]:                flags.append("CORE")
            if ticker in ["INFR", "BRIJ", "GILG", "IGLN"]: flags.append("SATELLITE")
            if ticker in ["EEM", "WSML"]:                 flags.append("OPPORTUNISTIC")
            p["flags"] = flags

            for band, policy in SIZING_POLICY.items():
                if ticker in policy["tickers"]:
                    p["sizing_band"]   = band
                    p["sizing_breach"] = not (policy["min_pct"] <= weight_pct <= policy["max_pct"])
                    break
            else:
                p["sizing_band"]   = "Unclassified"
                p["sizing_breach"] = False

        # Build allocation dict: each asset class as % of total portfolio value (incl. cash)
        alloc = {}
        for p in open_pos:
            ac = p["asset_class"]
            alloc[ac] = round(alloc.get(ac, 0) + p["mv_usd"] / total_val * 100, 2)

        # Add cash as its own allocation slice so the pie sums to 100%
        if cash > 0 and total_val > 0:
            alloc["Cash"] = round(cash / total_val * 100, 2)

        nav_series, bench_series = build_nav_curve(
            trades, fx_rates, cfg, cfg["benchmark"]
        )
        metrics = calc_metrics(nav_series, cfg["starting_capital"], rf_annual=rf_rate, closed_trades=closed)

        realised_total = sum(t.get("realised_pnl_usd", 0) for t in closed)

        # --- FX Exposure ---
        fx_exposure = {}
        for currency in ["USD", "GBP", "EUR"]:
            ccy_mv = sum(p["mv_usd"] for p in open_pos if p["currency"] == currency)
            fx_exposure[currency + "_pct"] = round(ccy_mv / total_val * 100, 2) if total_val else 0

        return jsonify({
            "portfolio_name":         cfg["portfolio_name"],
            "inception_date":         cfg["inception_date"],
            "benchmark":              cfg["benchmark"],
            "starting_capital":       cfg["starting_capital"],
            "current_value":          round(total_val, 2),
            "total_pnl":              round(total_mv - total_cost + realised_total, 2),
            "cash":                   round(max(cash, 0), 2),
            "metrics":                metrics,
            "rf_rate":                round(rf_rate * 100, 3),
            "open_positions":         open_pos,
            "closed_trades":          closed,
            "allocation":             alloc,
            "nav_series":             nav_series,
            "benchmark_series":       bench_series,
            "fx_rates":               fx_rates,
            "fx_exposure":            fx_exposure,
            "position_sizing_policy": SIZING_POLICY,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/price_history")
def price_history():
    from flask import request
    ticker = request.args.get("ticker", "SPY")
    period = request.args.get("period", "6mo")
    try:
        h = yf.Ticker(ticker).history(period=period)
        if h.empty:
            return jsonify([])
        result = [{"date": str(idx.date()), "close": round(float(row["Close"]), 4)}
                  for idx, row in h.iterrows()]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# TODO: Create a 'trade_rationale' tab in Google Sheets with columns:
# ticker | asset_class | entry_date | entry_rationale | exit_date |
# exit_rationale | realised_pnl_usd | lessons
@app.route("/api/trade_rationale")
def trade_rationale():
    try:
        trades, config_rows, manual_rows = get_sheet_data()
        # get_sheet_data only fetches trades, config, manual_prices.
        # We need to open the sheet again to get trade_rationale tab.
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                f.write(creds_json)
                tmp_path = f.name
            gc = gspread.service_account(filename=tmp_path)
        else:
            gc = gspread.service_account(filename=CREDS_FILE)
        sh = gc.open_by_key(SHEET_ID)
        try:
            rows = sh.worksheet("trade_rationale").get_all_records()
            return jsonify(rows)
        except:
            return jsonify([])
    except Exception as e:
        return jsonify([])

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

from flask import send_from_directory

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    app.run(debug=True, port=5000)
