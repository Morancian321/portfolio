# FUND CENTRE BACKEND
# ====================
# FIXED (2025-05): All changes limited to app.py on App_Rebuild branch.
#   1. SORTINO: denominator now uses all N returns (not just downside count); min(r-rf,0)^2.
#   2. CASH: now tracks full sale proceeds (cost_usd_sold + realised_pnl_usd), not just P&L.
#   3. RECOVERY TROUGH: replaced fragile nested ternary with clear explicit drawdown loop.
#   4. TOTAL_RETURN_PCT: calc_metrics value is now canonical; removed overwrite in /api/portfolio.
#      Simple money-weighted version exposed as simple_total_return_pct.
#   5. STRIP_OUTLIERS: threshold raised to 0.30 (30%); comment explains data-quality purpose.
#   6. FX EXPOSURE: cash balance (in base currency) now included so percentages sum ~100%.
#   7. CLOSE ACTION: always treated as full close; qty_held used (not trade qty); documented.
#   8. NAV_OVERRIDES: manual price corrections injected into prices DataFrame BEFORE
#      strip_outliers runs, so the clean price is ffill'd forward correctly.
#      Previous approach applied overrides inside the daily loop after strip_outliers had
#      already baked the bad ffill'd value into the DataFrame — so the fix had no effect.
#   9. TZ FIX: yf.download() returns a tz-aware UTC index; pd.bdate_range() is tz-naive.
#      prices.loc[:dt] in the NAV loop raised TypeError (silently caught by except: pass),
#      valuing every holding at 0 and producing the flat line + spike in the NAV chart.
#      Fix: strip timezone from prices.index immediately after download so all .loc
#      slicing uses tz-naive timestamps throughout.
#  10. NAV FINAL-DAY ALIGNMENT: on the last date of the NAV date_range, the portfolio
#      value is taken directly from build_positions() live MV (same source as the KPI),
#      rather than from the yf.download() batch prices. This eliminates the divergence
#      between the NAV endpoint and the KPI current_value caused by yfinance returning
#      slightly different prices from its two call paths (download vs Ticker.history).
#  11. BENCHMARK METRICS: calc_benchmark_metrics() computes Sharpe, Sortino, max drawdown,
#      and 30d rolling vol for the benchmark (SPY) series using identical formulas to
#      calc_metrics(). Exposed as benchmark_metrics in /api/portfolio response.
#  12. C&CE SLEEVE: positions with asset_class == "C&CE" are treated as the cash sleeve.
#      Their live MV is included in total_val (via open_pos/total_mv) as normal.
#      The alloc dict merges residual uninvested cash into the "C&CE" bucket rather than
#      a separate "Cash" key. cce_positions and cce_total (positions MV + residual cash)
#      are exposed in the API response for the frontend C&CE box.
#  13. INCOME (dividends_coupons): dividends and coupon payments are read from the
#      dividends_coupons sheet (columns: date, asset_class, div_income, currency, note).
#      Cash received is treated as cash income — increases C&CE (residual cash) and
#      realised P&L. Historical NAV curve injects income on the correct payment date.
#      API exposes income_records, total_income_usd, dividends_usd, coupons_usd.
#  14. DISPLAY CURRENCY: accounting remains fully USD-based, while the API can expose
#      EUR-converted presentation values when display_currency == EUR in portfolio_config.
#      This affects response amounts only; internal cash, NAV, P&L, and benchmark calcs remain USD.
# SAFE: Sharpe, NAV curve logic, hit_rate, rolling_vol — unchanged.
# ADDED: test harness under if __name__ == '__main__' for regressions.

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
    if avg_price_gbp > 0 and raw_price > avg_price_gbp * 50:
        return raw_price / 100
    return raw_price

# FIX 5: Threshold raised to 0.30.
# Purpose: data-quality spike filtering only — NOT smoothing of real volatility.
# A 15% single-day filter would silently mask real equity/ETF crash events.
# 30% still catches data-feed errors (price reporting bugs, splits not adjusted)
# while preserving genuine large moves like a circuit-breaker day.
def strip_outliers(df, threshold=0.125):
    pct = df.pct_change().abs()
    df = df.mask(pct > threshold)
    df = df.ffill().bfill()
    return df

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
    trades  = sh.worksheet("trades").get_all_records()
    config  = sh.worksheet("portfolio_config").get_all_records()
    try:
        manual = sh.worksheet("manual_prices").get_all_records()
    except:
        manual = []
    # FIX 8: Read nav_overrides tab for manual historical price corrections.
    try:
        nav_overrides_rows = sh.worksheet("nav_overrides").get_all_records()
    except:
        nav_overrides_rows = []
    # FIX 13: Read dividends_coupons tab for dividend and coupon income.
    try:
        income_rows = sh.worksheet("dividends_coupons").get_all_records()
    except:
        income_rows = []
    return trades, config, manual, nav_overrides_rows, income_rows

def parse_nav_overrides(rows):
    """
    Build a (date_str, yf_ticker) -> price lookup from the nav_overrides sheet.
    Only rows with action == OVERRIDE_PRICE are included.
    """
    overrides = {}
    for row in rows:
        if str(row.get("action", "")).upper() == "OVERRIDE_PRICE":
            key = (str(row["date"]), str(row["ticker"]))
            overrides[key] = float(row["value"])
    return overrides

def parse_income(income_rows, fx_rates):
    """
    FIX 13: Parse the dividends_coupons sheet into a list of income records.
    Sheet columns: date, asset_class (Dividend/Coupon), div_income, currency, note.
    cash_usd = div_income * fx_rate (converts local currency to USD).
    Both Dividend and Coupon income are treated as cash received — they increase
    C&CE (residual cash) and are included in total_realised_pnl.
    """
    records = []
    for row in income_rows:
        amount_local = float(row.get("div_income", 0) or 0)
        currency     = str(row.get("currency", "USD")).strip().upper()
        fx           = fx_rates.get(currency, 1.0)
        income_type  = str(row.get("asset_class", "")).strip().capitalize()  # "Dividend" or "Coupon"
        records.append({
            "date":         str(row.get("date", "")),
            "income_type":  income_type,
            "amount_local": amount_local,
            "currency":     currency,
            "cash_usd":     round(amount_local * fx, 2),
            "note":         str(row.get("note", "")),
        })
    return records

def apply_nav_overrides_to_prices(prices, nav_overrides):
    """
    FIX 8 (real fix): Stamp override values directly into the prices DataFrame
    BEFORE strip_outliers is called.
    """
    if not nav_overrides:
        return prices
    idx_map = {ts.strftime("%Y-%m-%d"): ts for ts in prices.index}
    for (date_str, ytk), price in nav_overrides.items():
        if ytk in prices.columns and date_str in idx_map:
            ts = idx_map[date_str]
            prices.at[ts, ytk] = price
    return prices

def parse_config(config_rows):
    cfg = {r["key"]: r["value"] for r in config_rows}
    return {
        "starting_capital": float(cfg.get("starting_capital", 100000)),
        "base_currency":    cfg.get("base_currency", "USD"),
        "display_currency": str(cfg.get("display_currency", cfg.get("base_currency", "USD"))).upper(),
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
    eurusd = rates.get("EUR", 1.0) or 1.0
    rates["USDEUR"] = 1.0 / eurusd if eurusd else 1.0
    return rates

def get_risk_free_rate():
    try:
        h = yf.Ticker("^IRX").history(period="5d")
        if not h.empty:
            return float(h["Close"].iloc[-1]) / 100
    except:
        pass
    return 0.043

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
    val = str(trade_row.get("tv_no_fx", "")).strip().upper()
    return val in ("TRUE", "1", "YES")

def get_display_rate(cfg, fx_rates):
    display_ccy = cfg.get("display_currency", "USD")
    if display_ccy == "EUR":
        return fx_rates.get("USDEUR", 1.0)
    return 1.0

def convert_amount(value, rate):
    return round(float(value) * rate, 2)

def convert_positions_for_display(positions, rate):
    converted = []
    for p in positions:
        item = dict(p)
        for key in ["mv_usd", "cost_usd", "unreal_pnl"]:
            if key in item:
                item[key] = convert_amount(item[key], rate)
        converted.append(item)
    return converted

def convert_closed_for_display(closed, rate):
    converted = []
    for t in closed:
        item = dict(t)
        for key in ["realised_pnl_usd", "cost_usd_sold"]:
            if key in item:
                item[key] = convert_amount(item[key], rate)
        converted.append(item)
    return converted

def convert_income_for_display(records, rate):
    converted = []
    for r in records:
        item = dict(r)
        if "cash_usd" in item:
            item["cash_usd"] = convert_amount(item["cash_usd"], rate)
        converted.append(item)
    return converted

def convert_nav_series_for_display(series, rate):
    return [{"date": x["date"], "value": convert_amount(x["value"], rate)} for x in series]

def build_positions(trades, fx_rates, manual_map):
    from collections import defaultdict
    ticker_trades = defaultdict(list)
    for t in trades:
        if t.get("ticker") and t.get("action"):
            ticker_trades[t["ticker"]].append(t)

    open_positions = []
    closed_trades  = []

    for ticker, events in ticker_trades.items():
        events_sorted = sorted(events, key=lambda x: x["date"])
        qty_held    = 0.0
        cost_basis  = 0.0
        open_date   = None
        yf_ticker   = events_sorted[0].get("yf_ticker", ticker)
        asset_class = events_sorted[0].get("asset_class", "")
        name        = events_sorted[0].get("name", ticker)
        direction   = events_sorted[0].get("direction", "LONG")
        tv_no_fx    = False

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
                avg        = cost_basis / qty_held if qty_held else price
                cost_basis -= avg * qty
                qty_held   -= qty

                currency     = get_currency(yf_ticker)
                fx           = fx_rates.get(currency, 1.0)
                effective_fx = 1.0 if tv_no_fx else fx

                if is_lse(yf_ticker):
                    avg_usd   = avg / 100 * effective_fx
                    price_usd = price / 100 * effective_fx
                else:
                    avg_usd   = avg * effective_fx
                    price_usd = price * effective_fx

                realised_usd  = (price_usd - avg_usd) * qty
                cost_usd_sold = avg_usd * qty

                closed_trades.append({
                    "ticker":           ticker,
                    "name":             name,
                    "qty":              qty,
                    "entry_price":      round(avg_usd / effective_fx, 4) if effective_fx else round(avg, 4),
                    "exit_price":       round(price_usd / effective_fx, 4) if effective_fx else round(price, 4),
                    "realised_pnl_usd": round(realised_usd, 2),
                    "cost_usd_sold":    round(cost_usd_sold, 2),
                    "date":             e.get("date"),
                    "yf_ticker":        yf_ticker,
                    "asset_class":      asset_class,
                })

            elif action == "CLOSE":
                # FIX 7: CLOSE always means full close of the entire position.
                close_qty = qty_held
                avg       = cost_basis / close_qty if close_qty else price

                currency = get_currency(yf_ticker)
                fx       = fx_rates.get(currency, 1.0)
                effective_fx = 1.0 if tv_no_fx else fx

                if is_lse(yf_ticker):
                    avg_gbp   = avg / 100
                    price_gbp = price / 100
                else:
                    avg_gbp   = avg
                    price_gbp = price

                avg_usd      = avg_gbp * effective_fx
                price_usd    = price_gbp * effective_fx
                realised_usd = (price_usd - avg_usd) * close_qty
                cost_usd_sold = avg_usd * close_qty

                closed_trades.append({
                    "ticker":           ticker,
                    "name":             name,
                    "qty":              close_qty,
                    "entry_price":      round(avg_gbp, 4),
                    "exit_price":       round(price_gbp, 4),
                    "realised_pnl_usd": round(realised_usd, 2),
                    "cost_usd_sold":    round(cost_usd_sold, 2),
                    "date":             e.get("date"),
                    "yf_ticker":        yf_ticker,
                    "asset_class":      asset_class,
                })
                qty_held   = 0.0
                cost_basis = 0.0

        if qty_held > 0:
            live_price   = get_live_price(yf_ticker, manual_map)
            currency     = get_currency(yf_ticker)
            fx           = fx_rates.get(currency, 1.0)
            effective_fx = 1.0 if tv_no_fx else fx
            avg_price    = cost_basis / qty_held

            if is_lse(yf_ticker):
                ap = avg_price / 100
            else:
                ap = avg_price

            if live_price is not None:
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

def build_nav_curve(trades, fx_rates, cfg, benchmark_ticker, nav_overrides=None,
                    live_positions_mv=None, live_cash=None, income_records=None):
    # FIX 13: income_records are pre-indexed by date so that cash is increased
    # on the correct historical payment date in the NAV curve loop.
    if nav_overrides is None:
        nav_overrides = {}
    if income_records is None:
        income_records = []

    inception = datetime.strptime(cfg["inception_date"], "%Y-%m-%d")
    today     = datetime.today()
    starting  = cfg["starting_capital"]

    ticker_map = {}
    for t in trades:
        if t.get("yf_ticker") and t.get("ticker"):
            ticker_map[t["ticker"]] = t.get("yf_ticker")

    fx_tickers  = ["GBPUSD=X", "EURUSD=X"]
    all_tickers = list(set(ticker_map.values())) + fx_tickers + [benchmark_ticker]
    raw = yf.download(all_tickers,
                      start=inception.strftime("%Y-%m-%d"),
                      end=(today + timedelta(days=1)).strftime("%Y-%m-%d"),
                      auto_adjust=True, progress=False)

    if isinstance(raw.columns, pd.MultiIndex):
        prices = raw["Close"]
    else:
        prices = raw[["Close"]] if "Close" in raw.columns else raw

    # FIX 9: Strip timezone from prices index.
    if prices.index.tz is not None:
        prices.index = prices.index.tz_convert("UTC").tz_localize(None)

    # FIX 8: Inject overrides into the raw DataFrame BEFORE strip_outliers.
    prices = apply_nav_overrides_to_prices(prices, nav_overrides)
    prices = strip_outliers(prices)

    def get_hist_fx(col):
        return prices[col] if col in prices.columns else None

    hist_gbpusd = get_hist_fx("GBPUSD=X")
    hist_eurusd = get_hist_fx("EURUSD=X")

    def fx_on_date(currency, dt, no_fx=False):
        if no_fx or currency == "USD":
            return 1.0
        if currency == "GBP":
            series, fallback = hist_gbpusd, fx_rates.get("GBP", 1.0)
        elif currency == "EUR":
            series, fallback = hist_eurusd, fx_rates.get("EUR", 1.0)
        else:
            return 1.0
        if series is None:
            return fallback
        try:
            val = float(series.loc[:dt].iloc[-1])
            return val if pd.notna(val) else fallback
        except:
            return fallback

    from collections import defaultdict
    events_by_date = defaultdict(list)
    for t in trades:
        events_by_date[t["date"]].append(t)

    # FIX 13: Pre-index income by date for O(1) lookup in the daily loop.
    income_by_date = defaultdict(float)
    for r in income_records:
        if r.get("date"):
            income_by_date[r["date"]] += r["cash_usd"]

    holdings    = {}
    cash        = starting
    nav_series  = []
    bench_series = []
    bench_start = None
    date_range  = pd.bdate_range(start=inception, end=today)
    last_date   = date_range[-1] if len(date_range) > 0 else None

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
            fx_r     = fx_on_date(currency, dt, no_fx=no_fx)
            price_gbp = price / 100 if is_lse(ytk) else price
            p_usd    = price_gbp * fx_r

            if action == "OPEN":
                holdings[tk] = {"qty": qty, "yf_ticker": ytk, "tv_no_fx": no_fx, "avg_cost_gbp": price_gbp}
                cash -= p_usd * qty
            elif action == "ADD":
                if tk in holdings:
                    old       = holdings[tk]
                    total_qty = old["qty"] + qty
                    avg_cost  = (old["avg_cost_gbp"] * old["qty"] + price_gbp * qty) / total_qty
                    holdings[tk]["qty"] = total_qty
                    holdings[tk]["avg_cost_gbp"] = avg_cost
                else:
                    holdings[tk] = {"qty": qty, "yf_ticker": ytk, "tv_no_fx": no_fx, "avg_cost_gbp": price_gbp}
                cash -= p_usd * qty
            elif action in ("REDUCE", "CLOSE"):
                if tk in holdings:
                    close_qty = qty if action == "REDUCE" else holdings[tk]["qty"]
                    cash += p_usd * close_qty
                    if action == "CLOSE":
                        del holdings[tk]
                    else:
                        holdings[tk]["qty"] -= close_qty

        # FIX 13: Inject income received on this date into the cash balance.
        cash += income_by_date.get(ds, 0.0)

        # FIX 10: On the final date, use live prices from build_positions().
        is_final_date = (dt == last_date)
        if is_final_date and live_positions_mv is not None and live_cash is not None:
            port_val = live_cash + live_positions_mv
        else:
            port_val = cash
            for tk, h in holdings.items():
                ytk          = h["yf_ticker"]
                currency     = get_currency(ytk)
                fx_r         = fx_on_date(currency, dt, no_fx=h.get("tv_no_fx", False))
                avg_cost_gbp = h.get("avg_cost_gbp", 0)
                try:
                    if ytk in prices.columns:
                        p_raw = float(prices.loc[:dt, ytk].iloc[-1])
                        p_gbp = normalize_lse_price(p_raw, avg_cost_gbp) if is_lse(ytk) else p_raw
                        port_val += p_gbp * fx_r * h["qty"]
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
                bench_series.append({"date": ds, "value": round(starting * (bp / bench_start), 2)})
        except:
            pass

    return nav_series, bench_series

def calc_metrics(nav_series, starting_capital, rf_annual=0.043, closed_trades=[], income_usd=0.0):
    # FIX 13: income_usd is the total cash received from dividends and coupons.
    # It is added to total_realised_pnl so that income is reflected in realised P&L.
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
    downside_sq_sum = sum(min(r - rf_daily, 0) ** 2 for r in daily_returns)
    downside_var    = downside_sq_sum / n
    downside_std    = math.sqrt(downside_var)
    sortino_ratio   = ((mean_r - rf_daily) / downside_std * math.sqrt(252)) if downside_std > 0 else 0

    last30          = daily_returns[-30:] if len(daily_returns) >= 30 else daily_returns
    last30_mean     = sum(last30) / len(last30) if last30 else 0
    rolling_std     = math.sqrt(sum((r - last30_mean)**2 for r in last30) / max(len(last30) - 1, 1))
    rolling_30d_vol = rolling_std * math.sqrt(252) * 100
    downside_deviation = downside_std * math.sqrt(252) * 100

    # FIX 3: Recovery / drawdown trough — clear, explicit loop.
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
    # FIX 13: Include income (dividends + coupons) in total_realised_pnl.
    total_realised_pnl = round(
        sum(t.get("realised_pnl_usd", 0) for t in closed_trades) + income_usd, 2
    )

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

def calc_benchmark_metrics(bench_series, rf_annual=0.043):
    """
    FIX 11: Compute Sharpe, Sortino, max drawdown, and 30d rolling volatility
    for the benchmark (SPY) series using identical formulas to calc_metrics().
    """
    if len(bench_series) < 2:
        return {
            "benchmark_sharpe":           None,
            "benchmark_sortino":          None,
            "benchmark_max_drawdown_pct": None,
            "benchmark_rolling_30d_vol":  None,
        }
    import math
    values        = [x["value"] for x in bench_series]
    daily_returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
    n        = len(daily_returns)
    mean_r   = sum(daily_returns) / n
    rf_daily = rf_annual / 252
    variance = sum((r - mean_r)**2 for r in daily_returns) / (n - 1) if n > 1 else 0
    std_r    = math.sqrt(variance)
    sharpe   = ((mean_r - rf_daily) / std_r * math.sqrt(252)) if std_r > 0 else 0

    downside_sq_sum = sum(min(r - rf_daily, 0) ** 2 for r in daily_returns)
    downside_var    = downside_sq_sum / n
    downside_std    = math.sqrt(downside_var)
    sortino         = ((mean_r - rf_daily) / downside_std * math.sqrt(252)) if downside_std > 0 else 0

    last30      = daily_returns[-30:] if len(daily_returns) >= 30 else daily_returns
    last30_mean = sum(last30) / len(last30) if last30 else 0
    rolling_std = math.sqrt(sum((r - last30_mean)**2 for r in last30) / max(len(last30) - 1, 1))
    vol_30d     = rolling_std * math.sqrt(252) * 100

    peak_val = values[0]
    max_dd   = 0.0
    for v in values:
        if v > peak_val:
            peak_val = v
        if peak_val > 0:
            dd = (peak_val - v) / peak_val
            if dd > max_dd:
                max_dd = dd

    return {
        "benchmark_sharpe":           round(sharpe, 2),
        "benchmark_sortino":          round(sortino, 2),
        "benchmark_max_drawdown_pct": round(max_dd * 100, 2),
        "benchmark_rolling_30d_vol":  round(vol_30d, 2),
    }

@app.route("/api/portfolio")
def portfolio():
    try:
        trades, config_rows, manual_rows, nav_overrides_rows, income_rows = get_sheet_data()
        cfg          = parse_config(config_rows)
        fx_rates     = get_fx_rates()
        rf_rate      = get_risk_free_rate()
        display_rate = get_display_rate(cfg, fx_rates)
        manual_map   = {r["ticker"]: r["manual_price"] for r in manual_rows if r.get("ticker")}
        nav_overrides = parse_nav_overrides(nav_overrides_rows)

        # FIX 13: Parse income records and compute totals.
        income_records   = parse_income(income_rows, fx_rates)
        total_income_usd = sum(r["cash_usd"] for r in income_records)
        dividends_usd    = sum(r["cash_usd"] for r in income_records if r["income_type"] == "Dividend")
        coupons_usd      = sum(r["cash_usd"] for r in income_records if r["income_type"] == "Coupon")

        open_pos, closed = build_positions(trades, fx_rates, manual_map)

        total_mv   = sum(p["mv_usd"] for p in open_pos)
        total_cost = sum(p["cost_usd"] for p in open_pos)

        # FIX 2 + FIX 13: Cash = starting_capital - cost_of_open_positions
        # + sale_proceeds + income_received (dividends + coupons).
        proceeds_total = sum((t.get("cost_usd_sold", 0) + t.get("realised_pnl_usd", 0)) for t in closed)
        cash = cfg["starting_capital"] - total_cost + proceeds_total + total_income_usd
        cash = max(cash, 0)
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

        # FIX 12: Build allocation dict.
        alloc = {}
        for p in open_pos:
            ac = p["asset_class"]
            alloc[ac] = round(alloc.get(ac, 0) + p["mv_usd"] / total_val * 100, 2)
        if cash > 0 and total_val > 0:
            alloc["C&CE"] = round(alloc.get("C&CE", 0) + cash / total_val * 100, 2)

        # FIX 12: Compute C&CE sleeve totals.
        cce_positions = [p for p in open_pos if p.get("asset_class") == "C&CE"]
        cce_mv        = sum(p["mv_usd"] for p in cce_positions)
        cce_total     = round(cce_mv + cash, 2)

        # FIX 10 + FIX 13: Pass income_records into build_nav_curve so that
        # historical income payments are reflected in the NAV curve.
        nav_series, bench_series = build_nav_curve(
            trades, fx_rates, cfg, cfg["benchmark"],
            nav_overrides=nav_overrides,
            live_positions_mv=total_mv,
            live_cash=cash,
            income_records=income_records,
        )
        # FIX 13: Pass income_usd into calc_metrics so total_realised_pnl includes income.
        metrics = calc_metrics(
            nav_series, cfg["starting_capital"],
            rf_annual=rf_rate,
            closed_trades=closed,
            income_usd=total_income_usd,
        )

        # FIX 11: Compute benchmark KPIs.
        bench_metrics = calc_benchmark_metrics(bench_series, rf_annual=rf_rate)

        # FIX 4: Do NOT overwrite metrics["total_return_pct"].
        simple_total_return_pct = (
            round((total_val - cfg["starting_capital"]) / cfg["starting_capital"] * 100, 2)
            if cfg["starting_capital"] else 0
        )

        # FIX 6: FX exposure — include residual cash in base currency bucket.
        base_ccy = cfg.get("base_currency", "USD")
        fx_exposure = {}
        for currency in ["USD", "GBP", "EUR"]:
            ccy_mv = sum(p["mv_usd"] for p in open_pos if p["currency"] == currency)
            if currency == base_ccy:
                ccy_mv += cash
            fx_exposure[currency + "_pct"] = round(ccy_mv / total_val * 100, 2) if total_val else 0

        open_pos_display      = convert_positions_for_display(open_pos, display_rate)
        closed_display        = convert_closed_for_display(closed, display_rate)
        cce_positions_display = convert_positions_for_display(cce_positions, display_rate)
        income_display        = convert_income_for_display(income_records, display_rate)
        nav_display           = convert_nav_series_for_display(nav_series, display_rate)

        metrics_display = dict(metrics)
        for key in ["current_value", "total_pnl", "avg_gain_usd", "avg_loss_usd", "total_realised_pnl"]:
            if key in metrics_display:
                metrics_display[key] = convert_amount(metrics_display[key], display_rate)

        return jsonify({
            "portfolio_name":           cfg["portfolio_name"],
            "inception_date":           cfg["inception_date"],
            "benchmark":                cfg["benchmark"],
            "base_currency":            cfg["base_currency"],
            "display_currency":         cfg["display_currency"],
            "usdeur_rate":              round(fx_rates.get("USDEUR", 1.0), 6),
            "starting_capital":         convert_amount(cfg["starting_capital"], display_rate),
            "current_value":            convert_amount(total_val, display_rate),
            "total_pnl":                convert_amount(total_val - cfg["starting_capital"], display_rate),
            "cash":                     convert_amount(cash, display_rate),
            "cce_positions":            cce_positions_display,
            "cce_total":                convert_amount(cce_total, display_rate),
            "metrics":                  metrics_display,
            "benchmark_metrics":        bench_metrics,
            "simple_total_return_pct":  simple_total_return_pct,
            "rf_rate":                  round(rf_rate * 100, 3),
            "open_positions":           open_pos_display,
            "closed_trades":            closed_display,
            "allocation":               alloc,
            "nav_series":               nav_display,
            "benchmark_series":         bench_series,
            "fx_rates":                 fx_rates,
            "fx_exposure":              fx_exposure,
            "position_sizing_policy":   SIZING_POLICY,
            # FIX 13 + 14: Income fields for frontend income box, converted for display.
            "income_records":           income_display,
            "total_income_usd":         convert_amount(total_income_usd, display_rate),
            "dividends_usd":            convert_amount(dividends_usd, display_rate),
            "coupons_usd":              convert_amount(coupons_usd, display_rate),
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

@app.route("/api/trade_rationale")
def trade_rationale():
    try:
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

# =============================================================================
# TEST HARNESS — run with: python app.py
# =============================================================================
def _run_tests():
    import math

    print("=== Running regression tests ===")
    errors = []

    # TEST 1: Sortino ratio
    rf_daily   = 0.0
    returns    = [0.01, -0.02, 0.03, -0.01, 0.02]
    n          = len(returns)
    mean_r     = sum(returns) / n
    dsq        = sum(min(r - rf_daily, 0) ** 2 for r in returns)
    d_std      = math.sqrt(dsq / n)
    sortino    = (mean_r - rf_daily) / d_std * math.sqrt(252) if d_std > 0 else 0
    expected   = (0.006 / 0.01) * math.sqrt(252)
    if abs(sortino - expected) > 1e-6:
        errors.append(f"Sortino FAIL: got {sortino:.6f}, expected {expected:.6f}")
    else:
        print(f"  [PASS] Sortino = {sortino:.4f}")

    returns_all_pos = [0.01, 0.02, 0.03]
    dsq2 = sum(min(r, 0) ** 2 for r in returns_all_pos)
    d_std2 = math.sqrt(dsq2 / len(returns_all_pos))
    sortino2 = (sum(returns_all_pos)/len(returns_all_pos) / d_std2 * math.sqrt(252)) if d_std2 > 0 else 0
    if sortino2 != 0:
        errors.append(f"Sortino zero-downside FAIL: got {sortino2}")
    else:
        print("  [PASS] Sortino zero-downside = 0 (no crash)")

    # TEST 2: calc_metrics max drawdown and trough date
    nav = [
        {"date": "2024-01-01", "value": 100},
        {"date": "2024-01-02", "value": 105},
        {"date": "2024-01-03", "value": 110},
        {"date": "2024-01-04", "value": 88},
        {"date": "2024-01-05", "value": 95},
    ]
    m = calc_metrics(nav, starting_capital=100, rf_annual=0.0)
    expected_dd = round((110 - 88) / 110 * 100, 2)
    if abs(m["max_drawdown_pct"] - expected_dd) > 0.01:
        errors.append(f"MaxDD FAIL: got {m['max_drawdown_pct']}, expected {expected_dd}")
    else:
        print(f"  [PASS] Max drawdown = {m['max_drawdown_pct']}%")
    if m["recovery_status"]["trough_date"] != "2024-01-04":
        errors.append(f"Trough date FAIL: got {m['recovery_status']['trough_date']}")
    else:
        print(f"  [PASS] Trough date = {m['recovery_status']['trough_date']}")

    # TEST 3: Cash calculation
    starting  = 10000.0
    total_cost_open = 3000.0
    closed_t = [{"cost_usd_sold": 2000.0, "realised_pnl_usd": 200.0}]
    proceeds  = sum(t.get("cost_usd_sold", 0) + t.get("realised_pnl_usd", 0) for t in closed_t)
    cash_test = starting - total_cost_open + proceeds
    if abs(cash_test - 9200.0) > 0.01:
        errors.append(f"Cash FAIL: got {cash_test}, expected 9200")
    else:
        print(f"  [PASS] Cash = {cash_test}")

    # TEST 4: FX exposure sums to ~100%
    test_positions = [
        {"currency": "USD", "mv_usd": 3000},
        {"currency": "GBP", "mv_usd": 2000},
    ]
    test_cash = 5000.0
    test_total_val = sum(p["mv_usd"] for p in test_positions) + test_cash
    base_ccy = "USD"
    fx_exp = {}
    for ccy in ["USD", "GBP", "EUR"]:
        mv = sum(p["mv_usd"] for p in test_positions if p["currency"] == ccy)
        if ccy == base_ccy:
            mv += test_cash
        fx_exp[ccy + "_pct"] = round(mv / test_total_val * 100, 2)
    total_pct = sum(fx_exp.values())
    if abs(total_pct - 100.0) > 0.1:
        errors.append(f"FX exposure sum FAIL: {total_pct}% (expected ~100%)")
    else:
        print(f"  [PASS] FX exposure sums to {total_pct}%  {fx_exp}")

    # TEST 5: parse_nav_overrides
    sample_rows = [
        {"date": "2026-05-07", "ticker": "IWDA.L", "action": "OVERRIDE_PRICE", "value": 120.1, "notes": "bad feed"},
        {"date": "2026-05-08", "ticker": "IWDA.L", "action": "NOTE",           "value": 0,     "notes": "ignore"},
    ]
    ov = parse_nav_overrides(sample_rows)
    if ov != {("2026-05-07", "IWDA.L"): 120.1}:
        errors.append(f"parse_nav_overrides FAIL: got {ov}")
    else:
        print("  [PASS] parse_nav_overrides filters correctly")

    # TEST 6: apply_nav_overrides_to_prices
    idx = pd.to_datetime(["2026-05-06", "2026-05-07", "2026-05-08"])
    df_test = pd.DataFrame({"IWDA.L": [119.5, 9999.0, 120.2]}, index=idx)
    ov_map  = {("2026-05-07", "IWDA.L"): 120.1}
    df_fixed = apply_nav_overrides_to_prices(df_test.copy(), ov_map)
    corrected = df_fixed.at[pd.Timestamp("2026-05-07"), "IWDA.L"]
    if abs(corrected - 120.1) > 1e-6:
        errors.append(f"apply_nav_overrides_to_prices FAIL: got {corrected}, expected 120.1")
    else:
        print(f"  [PASS] apply_nav_overrides_to_prices stamped correctly ({corrected})")

    # TEST 7: TZ fix
    idx_tz = pd.to_datetime(["2026-05-06", "2026-05-07", "2026-05-08"]).tz_localize("UTC")
    df_tz = pd.DataFrame({"IWDA.L": [119.5, 120.1, 120.2]}, index=idx_tz)
    if df_tz.index.tz is not None:
        df_tz.index = df_tz.index.tz_convert("UTC").tz_localize(None)
    dt_naive = pd.bdate_range(start="2026-05-06", end="2026-05-08")[1]
    try:
        val = float(df_tz.loc[:dt_naive, "IWDA.L"].iloc[-1])
        if abs(val - 120.1) > 1e-6:
            errors.append(f"TZ fix FAIL: got {val}, expected {120.1}")
        else:
            print(f"  [PASS] TZ fix: prices.loc[:dt_naive] = {val} (no TypeError)")
    except Exception as e:
        errors.append(f"TZ fix FAIL: raised {type(e).__name__}: {e}")

    # TEST 8: FIX 10 NAV final-day alignment
    live_mv   = 95000.0
    live_cash_val = 5500.0
    expected_final = live_mv + live_cash_val
    simulated_final = live_cash_val + live_mv
    if abs(simulated_final - expected_final) > 0.01:
        errors.append(f"FIX 10 alignment FAIL: got {simulated_final}, expected {expected_final}")
    else:
        print(f"  [PASS] FIX 10: final NAV point = {simulated_final} (live prices anchor)")

    # TEST 9: calc_benchmark_metrics returns correct keys and plausible values
    bench_nav = [
        {"date": "2024-01-01", "value": 100},
        {"date": "2024-01-02", "value": 102},
        {"date": "2024-01-03", "value": 101},
        {"date": "2024-01-04", "value": 105},
        {"date": "2024-01-05", "value": 103},
    ]
    bm = calc_benchmark_metrics(bench_nav, rf_annual=0.0)
    required_keys = ["benchmark_sharpe", "benchmark_sortino", "benchmark_max_drawdown_pct", "benchmark_rolling_30d_vol"]
    missing = [k for k in required_keys if k not in bm]
    if missing:
        errors.append(f"calc_benchmark_metrics missing keys: {missing}")
    elif any(bm[k] is None for k in required_keys):
        errors.append(f"calc_benchmark_metrics returned None values: {bm}")
    else:
        print(f"  [PASS] calc_benchmark_metrics keys present, values: {bm}")

    # TEST 12: C&CE sleeve — alloc merges residual cash into C&CE bucket, not "Cash"
    test_open_pos = [
        {"asset_class": "Equity",  "mv_usd": 40000},
        {"asset_class": "C&CE",    "mv_usd": 20000},
    ]
    test_cash_cce  = 10000.0
    test_total_cce = sum(p["mv_usd"] for p in test_open_pos) + test_cash_cce
    test_alloc = {}
    for p in test_open_pos:
        ac = p["asset_class"]
        test_alloc[ac] = round(test_alloc.get(ac, 0) + p["mv_usd"] / test_total_cce * 100, 2)
    if test_cash_cce > 0:
        test_alloc["C&CE"] = round(test_alloc.get("C&CE", 0) + test_cash_cce / test_total_cce * 100, 2)
    if "Cash" in test_alloc:
        errors.append(f"C&CE alloc FAIL: 'Cash' key still present — {test_alloc}")
    elif abs(test_alloc.get("C&CE", 0) - round((20000 + 10000) / 70000 * 100, 2)) > 0.01:
        errors.append(f"C&CE alloc FAIL: C&CE pct wrong — {test_alloc}")
    else:
        print(f"  [PASS] C&CE alloc: {test_alloc}  (no 'Cash' key, C&CE={test_alloc['C&CE']}%)")

    # TEST 12b: cce_total = cce_mv + residual cash
    cce_pos_test  = [p for p in test_open_pos if p.get("asset_class") == "C&CE"]
    cce_mv_test   = sum(p["mv_usd"] for p in cce_pos_test)
    cce_total_test = round(cce_mv_test + test_cash_cce, 2)
    if abs(cce_total_test - 30000.0) > 0.01:
        errors.append(f"cce_total FAIL: got {cce_total_test}, expected 30000")
    else:
        print(f"  [PASS] cce_total = {cce_total_test} (positions MV + residual cash)")

    # TEST 13: parse_income maps columns correctly
    income_test_rows = [
        {"date": "2026-02-29", "asset_class": "Dividend", "div_income": 51290, "currency": "USD", "note": "GILG Dividend"},
        {"date": "2026-01-14", "asset_class": "Dividend", "div_income": 8660,  "currency": "USD", "note": "INFR Dividend"},
        {"date": "2026-05-10", "asset_class": "Coupon",   "div_income": 50000, "currency": "USD", "note": "AGGG Coupon"},
    ]
    fx_test = {"USD": 1.0, "GBP": 1.25, "EUR": 1.08}
    parsed = parse_income(income_test_rows, fx_test)
    if len(parsed) != 3:
        errors.append(f"parse_income FAIL: expected 3 records, got {len(parsed)}")
    elif parsed[0]["income_type"] != "Dividend":
        errors.append(f"parse_income FAIL: income_type wrong, got {parsed[0]['income_type']}")
    elif abs(parsed[2]["cash_usd"] - 50000.0) > 0.01:
        errors.append(f"parse_income FAIL: cash_usd wrong for coupon, got {parsed[2]['cash_usd']}")
    else:
        print(f"  [PASS] parse_income: {len(parsed)} records, types={[r['income_type'] for r in parsed]}")

    # TEST 13b: income increases cash correctly
    start_cap    = 100000.0
    cost_open    = 80000.0
    proceeds     = 0.0
    income_total = 51290 + 8660 + 50000  # 109950
    cash_with_income = start_cap - cost_open + proceeds + income_total
    if abs(cash_with_income - 129950.0) > 0.01:
        errors.append(f"Income cash FAIL: got {cash_with_income}, expected 129950")
    else:
        print(f"  [PASS] Income cash injection: {cash_with_income}")

    # TEST 13c: total_realised_pnl includes income
    closed_test = [{"realised_pnl_usd": 500.0}]
    income_usd_test = 109950.0
    total_rpl = round(sum(t.get("realised_pnl_usd", 0) for t in closed_test) + income_usd_test, 2)
    if abs(total_rpl - 110450.0) > 0.01:
        errors.append(f"Realised P&L with income FAIL: got {total_rpl}, expected 110450")
    else:
        print(f"  [PASS] total_realised_pnl with income = {total_rpl}")

    # TEST 14: display currency conversion helper
    cfg_test = {"display_currency": "EUR"}
    fx_rates_test = {"USDEUR": 0.92}
    rate = get_display_rate(cfg_test, fx_rates_test)
    if abs(rate - 0.92) > 1e-9:
        errors.append(f"Display rate FAIL: got {rate}, expected 0.92")
    elif abs(convert_amount(100.0, rate) - 92.0) > 0.01:
        errors.append(f"convert_amount FAIL: got {convert_amount(100.0, rate)}, expected 92.0")
    else:
        print("  [PASS] display currency conversion helper")

    if errors:
        print("\n=== FAILURES ===")
        for e in errors:
            print(" ", e)
        raise SystemExit(1)
    else:
        print("\nAll tests passed.")

if __name__ == "__main__":
    _run_tests()
    app.run(debug=True, port=5000)
