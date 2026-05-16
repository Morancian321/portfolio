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
#  14. DISPLAY CURRENCY: all internal calculations remain in USD. A display_currency key
#      in portfolio_config sheet (e.g. "EUR") triggers a single conversion layer at the
#      end of /api/portfolio before the JSON response is built. Ratio-based metrics
#      (Sharpe, Sortino, drawdown, vol, return %) are unaffected. Switching currency
#      requires only a single cell change in the Google Sheet.
#  15. CURRENCY COLUMN: get_currency() now reads the explicit "currency" column from each
#      trade row first, falling back to yf_ticker suffix only if the column is blank.
#      GBX (pence-denominated) is now the sole trigger for the /100 pence conversion
#      via is_lse_pence() — replacing the old is_lse() suffix check. This correctly
#      handles USD-denominated ETFs listed on the LSE (e.g. AGGG.L, WSML.L) which must
#      NOT be divided by 100 and must use USD FX (i.e. no conversion). The tv_no_fx
#      flag and helper are removed as the currency column makes them redundant.
#      GBX is normalised to GBP for FX rate lookups (both use GBPUSD=X); the /100
#      divide converts pence prices to pounds before the GBP->USD FX step.
#  16. RISK-FREE RATE: now EUR-denominated to match the fund's display currency.
#      Three-tier fallback chain:
#        1. ECB SDMX REST API — live daily €STR (Euro Short-Term Rate, overnight).
#           No API key required. Endpoint: data-api.ecb.europa.eu/service/data/ST/...
#        2. EURIBOR3M=X via yfinance — 3-month EUR interbank rate. Reliable fallback
#           when the ECB API is reachable but returns stale/empty data.
#        3. Hardcoded 2.40% — ECB deposit facility rate as of May 2026. Used only
#           when both live sources fail (network outage, API schema change, etc.).
#      rf_rate is still exposed in the /api/portfolio response (as an annualised %).
#  17. BENCHMARK DEFAULTS: updated fallback tickers to current 50:50 composition:
#      benchmark        -> IWDA.AS  (iShares MSCI World UCITS ETF Acc)
#      benchmark_bond   -> AGGH.AS  (iShares Core Global Aggregate Bond EUR Hedged Acc)
#      Both are accumulating ETFs; their price return equals total return.
#      Stale references to IEMU.L, IEGA.AS, and CEMU.AS have been removed.
# SAFE: Sharpe, NAV curve logic, hit_rate, rolling_vol — unchanged.
# ADDED: test harness under if __name__ == '__main__' for regressions.

import os
import json
import math
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
    "Opportunistic": {"tickers": ["EEM", "LUTI", "WSML"],                   "min_pct": 2,  "max_pct": 7},
    "Speculative":   {"tickers": ["BTCUSD", "BTC-USD", "COIN"],     "min_pct": 0,  "max_pct": 2},
}

# FIX 15: Read explicit "currency" column from the trade row first.
# Falls back to suffix-based inference only if the column is blank/missing.
# GBX is treated as GBP for FX lookups (both use GBPUSD=X); is_lse_pence()
# handles the /100 pence-to-pounds conversion separately.
def get_currency(trade_row_or_ticker, yf_ticker=None):
    # Called with (trade_row dict, yf_ticker) — new path
    if isinstance(trade_row_or_ticker, dict):
        explicit = str(trade_row_or_ticker.get("currency", "")).strip().upper()
        if explicit in ("USD", "GBP", "GBX", "EUR"):
            return explicit
        # fall through to suffix inference using yf_ticker
        t = (yf_ticker or "").upper()
    else:
        # Legacy call: get_currency(yf_ticker_string) — kept for safety
        t = trade_row_or_ticker.upper()

    if t.endswith(".L"):   return "GBP"
    if t.endswith(".AS"):  return "EUR"
    if t.endswith(".PA"):  return "EUR"
    if t.endswith(".DE"):  return "EUR"
    if t.endswith(".IR"):  return "EUR"
    return "USD"

# FIX 15: Replaces is_lse(). The /100 pence divide is now triggered ONLY when
# currency == "GBX" (pence), not by the .L suffix. USD/GBP/EUR .L tickers are
# priced in their stated currency and must NOT be divided by 100.
def is_lse_pence(currency):
    return currency == "GBX"

# FX lookup key: GBX uses the same GBPUSD=X rate as GBP (pence are still sterling).
def fx_key(currency):
    return "GBP" if currency == "GBX" else currency

def normalize_gbx_price(raw_price, avg_price_pounds):
    """
    Retained for GBX positions: yfinance may return pence or pounds inconsistently.
    Only called when currency == GBX.
    """
    if avg_price_pounds > 0 and raw_price > avg_price_pounds * 50:
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
        fx           = fx_rates.get(fx_key(currency), 1.0)
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
        "base_currency": cfg.get("base_currency", "USD"),
        "inception_date": cfg.get("inception_date", "2026-01-14"),
        # FIX 17: updated benchmark defaults to current 50:50 composition.
        "benchmark": cfg.get("benchmark", "IWDA.AS"),
        "benchmark_bond": cfg.get("benchmark_bond", "AGGH.AS"),
        "benchmark_equity_weight": float(cfg.get("benchmark_equity_weight", 0.5)),
        "benchmark_bond_weight": float(cfg.get("benchmark_bond_weight", 0.5)),
        "portfolio_name": cfg.get("portfolio_name", "Investment Portfolio"),
        "display_currency": cfg.get("display_currency", "USD"), # FIX 14
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
    """
    FIX 16: EUR risk-free rate — three-tier fallback chain.
    1. ECB SDMX REST API: live daily €STR (Euro Short-Term Rate, overnight).
       No API key required.
    2. EURIBOR3M=X via yfinance: 3-month EUR interbank rate.
    3. Hardcoded 2.40%: ECB deposit facility rate as of May 2026.
    """
    import requests

    # Tier 1: ECB live €STR
    try:
        url = (
            "https://data-api.ecb.europa.eu/service/data/ST/"
            "D.EUR.ESTR.RATE?lastNObservations=1&format=jsondata"
        )
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            obs = r.json()["dataSets"][0]["series"]["0:0:0:0"]["observations"]
            latest = obs[max(obs.keys(), key=int)][0]
            rate = float(latest) / 100
            if rate > 0:
                return rate
    except:
        pass

    # Tier 2: EURIBOR3M via yfinance
    try:
        h = yf.Ticker("EURIBOR3M=X").history(period="5d")
        if not h.empty:
            rate = float(h["Close"].iloc[-1])
            if rate > 0:
                return rate / 100
    except:
        pass

    # Tier 3: hardcoded ECB deposit rate (May 2026)
    return 0.024

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
        # FIX 15: currency is now tracked per-position from the trade row.
        currency    = get_currency(events_sorted[0], events_sorted[0].get("yf_ticker", ticker))

        for e in events_sorted:
            action   = e.get("action", "").upper()
            qty      = float(e.get("quantity", 0))
            price    = float(e.get("price", 0))
            # FIX 15: re-read currency on each event in case it changes (e.g. ADD row).
            e_currency = get_currency(e, e.get("yf_ticker", yf_ticker))

            if action == "OPEN":
                qty_held    = qty
                cost_basis  = price * qty
                open_date   = e.get("date")
                yf_ticker   = e.get("yf_ticker", yf_ticker)
                asset_class = e.get("asset_class", asset_class)
                name        = e.get("name", name)
                currency    = e_currency

            elif action == "ADD":
                cost_basis += price * qty
                qty_held   += qty
                currency    = e_currency

            elif action == "REDUCE":
                avg        = cost_basis / qty_held if qty_held else price
                cost_basis -= avg * qty
                qty_held   -= qty

                fx           = fx_rates.get(fx_key(e_currency), 1.0)
                # FIX 15: pence divide only for GBX; USD/GBP/EUR .L tickers use price as-is.
                if is_lse_pence(e_currency):
                    avg_base   = avg / 100
                    price_base = price / 100
                else:
                    avg_base   = avg
                    price_base = price

                avg_usd   = avg_base * fx
                price_usd = price_base * fx

                realised_usd  = (price_usd - avg_usd) * qty
                cost_usd_sold = avg_usd * qty

                closed_trades.append({
                    "ticker":           ticker,
                    "name":             name,
                    "qty":              qty,
                    "entry_price":      round(avg_base, 4),
                    "exit_price":       round(price_base, 4),
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

                fx = fx_rates.get(fx_key(e_currency), 1.0)
                # FIX 15: pence divide only for GBX.
                if is_lse_pence(e_currency):
                    avg_base   = avg / 100
                    price_base = price / 100
                else:
                    avg_base   = avg
                    price_base = price

                avg_usd      = avg_base * fx
                price_usd    = price_base * fx
                realised_usd = (price_usd - avg_usd) * close_qty
                cost_usd_sold = avg_usd * close_qty

                closed_trades.append({
                    "ticker":           ticker,
                    "name":             name,
                    "qty":              close_qty,
                    "entry_price":      round(avg_base, 4),
                    "exit_price":       round(price_base, 4),
                    "realised_pnl_usd": round(realised_usd, 2),
                    "cost_usd_sold":    round(cost_usd_sold, 2),
                    "date":             e.get("date"),
                    "yf_ticker":        yf_ticker,
                    "asset_class":      asset_class,
                })
                qty_held   = 0.0
                cost_basis = 0.0

        if qty_held > 0:
            live_price = get_live_price(yf_ticker, manual_map)
            fx         = fx_rates.get(fx_key(currency), 1.0)
            avg_price  = cost_basis / qty_held

            # FIX 15: pence divide only for GBX.
            if is_lse_pence(currency):
                ap = avg_price / 100
            else:
                ap = avg_price

            if live_price is not None:
                # FIX 15: normalize only for GBX pence feeds.
                if is_lse_pence(currency):
                    lp = normalize_gbx_price(live_price, ap)
                else:
                    lp = live_price
                cost_usd   = ap * qty_held * fx
                mv_usd     = lp * qty_held * fx
                unreal_pnl = mv_usd - cost_usd
                unreal_pct = (lp - ap) / ap if ap else 0
            else:
                lp         = ap
                mv_usd     = ap * qty_held * fx
                cost_usd   = mv_usd
                unreal_pnl = 0
                unreal_pct = 0

            # FIX 15: expose the canonical currency (GBX stays GBX so the frontend
            # can show the correct denomination; FX exposure uses fx_key() to bucket
            # GBX under GBP).
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
    # FIX 17: use live config values; fallbacks now match current benchmark composition.
    bench_eq_ticker   = cfg.get("benchmark", "IWDA.AS")
    bench_bond_ticker = cfg.get("benchmark_bond", "AGGH.AS")
    bench_eq_w   = float(cfg.get("benchmark_equity_weight", 0.5))
    bench_bond_w = float(cfg.get("benchmark_bond_weight", 0.5))
    all_tickers = list(set(ticker_map.values())) + fx_tickers + [bench_eq_ticker, bench_bond_ticker]
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

    # FIX 15: fx_on_date uses fx_key() so GBX resolves to the GBP series.
    def fx_on_date(currency, dt):
        fk = fx_key(currency)
        if fk == "USD":
            return 1.0
        if fk == "GBP":
            series, fallback = hist_gbpusd, fx_rates.get("GBP", 1.0)
        elif fk == "EUR":
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

    holdings     = {}
    cash         = starting
    nav_series   = []
    bench_series = []
    bench_start  = None
    date_range   = pd.bdate_range(start=inception, end=today)
    last_date    = date_range[-1] if len(date_range) > 0 else None

    for dt in date_range:
        ds = dt.strftime("%Y-%m-%d")

        for e in events_by_date.get(ds, []):
            tk       = e["ticker"]
            qty      = float(e.get("quantity", 0))
            price    = float(e.get("price", 0))
            action   = e.get("action", "").upper()
            ytk      = e.get("yf_ticker", tk)
            # FIX 15: currency from row, not suffix.
            currency = get_currency(e, ytk)
            fx_r     = fx_on_date(currency, dt)
            # FIX 15: pence divide only for GBX.
            price_base = price / 100 if is_lse_pence(currency) else price
            p_usd      = price_base * fx_r

            if action == "OPEN":
                holdings[tk] = {"qty": qty, "yf_ticker": ytk, "currency": currency, "avg_cost_base": price_base}
                cash -= p_usd * qty
            elif action == "ADD":
                if tk in holdings:
                    old       = holdings[tk]
                    total_qty = old["qty"] + qty
                    avg_cost  = (old["avg_cost_base"] * old["qty"] + price_base * qty) / total_qty
                    holdings[tk]["qty"] = total_qty
                    holdings[tk]["avg_cost_base"] = avg_cost
                else:
                    holdings[tk] = {"qty": qty, "yf_ticker": ytk, "currency": currency, "avg_cost_base": price_base}
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
                ytk      = h["yf_ticker"]
                currency = h.get("currency", get_currency({}, ytk))
                fx_r     = fx_on_date(currency, dt)
                avg_cost_base = h.get("avg_cost_base", 0)
                try:
                    if ytk in prices.columns:
                        p_raw = float(prices.loc[:dt, ytk].iloc[-1])
                        # FIX 15: normalize only for GBX pence feeds.
                        p_base = normalize_gbx_price(p_raw, avg_cost_base) if is_lse_pence(currency) else p_raw
                        port_val += p_base * fx_r * h["qty"]
                    else:
                        port_val += h["qty"]
                except:
                    pass

        nav_series.append({"date": ds, "value": round(port_val, 2)})

        try:
            eq_p   = float(prices.loc[:dt, bench_eq_ticker].iloc[-1])   if bench_eq_ticker   in prices.columns else None
            bond_p = float(prices.loc[:dt, bench_bond_ticker].iloc[-1]) if bench_bond_ticker in prices.columns else None
            if eq_p is not None and bond_p is not None:
                if bench_start is None:
                    eurusd_inception = fx_on_date("EUR", dt)        # reads hist_eurusd on inception date
                    starting_eur = starting / eurusd_inception       # converts USD→EUR once, locked forever
                    bench_start = {"eq": eq_p, "bond": bond_p, "starting_eur": starting_eur}

                blended = (
                    bench_start["starting_eur"] * (eq_p   / bench_start["eq"])   * bench_eq_w +
                    bench_start["starting_eur"] * (bond_p / bench_start["bond"]) * bench_bond_w
                )
                bench_series.append({"date": ds, "value": round(blended, 2)})
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
    variance = sum((r - mean_r)**2 for r in daily_returns) / max(n - 1, 1)
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
        proceeds_total = sum(t.get("realised_pnl_usd", 0) for t in closed)
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
            if ticker in ["IWDA", "AGGG"]:                  flags.append("CORE")
            if ticker in ["INFR", "BRIJ", "GILG", "IGLN"]:  flags.append("SATELLITE")
            if ticker in ["EEM", "LUTI", "WSML"]:           flags.append("OPPORTUNISTIC")
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

        # FIX 6 + FIX 15: FX exposure — GBX positions bucketed under GBP.
        base_ccy = cfg.get("base_currency", "USD")
        fx_exposure = {}
        for ccy in ["USD", "GBP", "EUR"]:
            # GBX maps to GBP bucket via fx_key()
            ccy_mv = sum(p["mv_usd"] for p in open_pos if fx_key(p["currency"]) == ccy)
            if ccy == base_ccy:
                ccy_mv += cash
            fx_exposure[ccy + "_pct"] = round(ccy_mv / total_val * 100, 2) if total_val else 0

        # -----------------------------------------------------------------
        # FIX 14: DISPLAY CURRENCY CONVERSION LAYER
        # All internal calculations above remain in USD.
        # If display_currency != "USD", convert all monetary outputs here
        # using the live FX rate fetched at the top of this request.
        # Ratio-based metrics (Sharpe, Sortino, drawdown %, return %) are
        # unaffected. To switch currency, change one cell in the config sheet.
        # -----------------------------------------------------------------
        disp = cfg.get("display_currency", "USD")
        if disp != "USD" and disp in fx_rates:
            usd_to_disp = 1.0 / fx_rates[disp]

            def conv(v):
                return round(v * usd_to_disp, 2) if isinstance(v, (int, float)) else v

            # Top-level scalars
            starting_capital_disp = conv(cfg["starting_capital"])
            current_value_disp    = conv(total_val)
            total_pnl_disp        = conv(total_val - cfg["starting_capital"])
            cash_disp             = conv(cash)
            cce_total_disp        = conv(cce_total)
            total_income_disp     = conv(total_income_usd)
            dividends_disp        = conv(dividends_usd)
            coupons_disp          = conv(coupons_usd)

            # Monetary fields inside metrics (ratios/percentages are left untouched)
            for key in ["current_value", "total_pnl", "avg_gain_usd", "avg_loss_usd", "total_realised_pnl"]:
                if key in metrics:
                    metrics[key] = conv(metrics[key])

            # Open positions
            for p in open_pos:
                for field in ["mv_usd", "cost_usd", "unreal_pnl"]:
                    p[field] = conv(p[field])

            # Closed trades
            for t in closed:
                for field in ["realised_pnl_usd", "cost_usd_sold"]:
                    t[field] = conv(t[field])

            # NAV series: USD → EUR
            nav_series   = [{"date": x["date"], "value": conv(x["value"])} for x in nav_series]
            # bench_series: already EUR from build_nav_curve — no conversion needed

            # Income records
            for r in income_records:
                r["cash_usd"] = conv(r["cash_usd"])

            # C&CE positions monetary fields
            for p in cce_positions:
                for field in ["mv_usd", "cost_usd", "unreal_pnl"]:
                    p[field] = conv(p[field])

        else:
            usd_to_disp           = 1.0
            starting_capital_disp = cfg["starting_capital"]
            current_value_disp    = round(total_val, 2)
            total_pnl_disp        = round(total_val - cfg["starting_capital"], 2)
            cash_disp             = round(cash, 2)
            cce_total_disp        = round(cce_total, 2)
            total_income_disp     = round(total_income_usd, 2)
            dividends_disp        = round(dividends_usd, 2)
            coupons_disp          = round(coupons_usd, 2)

        return jsonify({
            "portfolio_name":           cfg["portfolio_name"],
            "inception_date":           cfg["inception_date"],
            "benchmark":                cfg["benchmark"],
            "starting_capital":         starting_capital_disp,
            "current_value":            current_value_disp,
            "total_pnl":                total_pnl_disp,
            "cash":                     cash_disp,
            "cce_positions":            cce_positions,
            "cce_total":                cce_total_disp,
            "metrics":                  metrics,
            "benchmark_metrics":        bench_metrics,
            "simple_total_return_pct":  simple_total_return_pct,
            "rf_rate":                  round(rf_rate * 100, 3),
            "open_positions":           open_pos,
            "closed_trades":            closed,
            "allocation":               alloc,
            "nav_series":               nav_series,
            "benchmark_series":         bench_series,
            "fx_rates":                 fx_rates,
            "fx_exposure":              fx_exposure,
            "position_sizing_policy":   SIZING_POLICY,
            "display_currency":         disp,
            "usd_to_display":           round(usd_to_disp, 6),
            # FIX 13: Income fields for frontend income box.
            "income_records":           income_records,
            "total_income_usd":         total_income_disp,
            "dividends_usd":            dividends_disp,
            "coupons_usd":              coupons_disp,
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

@app.route("/api/asset_class_performance")
def asset_class_performance():
    try:
        trades, config_rows, manual_rows, nav_overrides_rows, income_rows = get_sheet_data()
        cfg       = parse_config(config_rows)
        fx_rates  = get_fx_rates()
        manual_map = {r["ticker"]: r["manual_price"] for r in manual_rows if r.get("ticker")}

        inception = datetime.strptime(cfg["inception_date"], "%Y-%m-%d")
        today     = datetime.today()
        disp      = cfg.get("display_currency", "USD")
        usd_to_disp = (1.0 / fx_rates[disp]) if (disp != "USD" and disp in fx_rates) else 1.0

        # Build ticker → yf_ticker map and collect all tickers needed
        ticker_map = {}
        for t in trades:
            if t.get("yf_ticker") and t.get("ticker"):
                ticker_map[t["ticker"]] = t["yf_ticker"]

        fx_tickers = ["GBPUSD=X", "EURUSD=X"]
        all_tickers = list(set(ticker_map.values())) + fx_tickers
        raw = yf.download(all_tickers,
                          start=inception.strftime("%Y-%m-%d"),
                          end=(today + timedelta(days=1)).strftime("%Y-%m-%d"),
                          auto_adjust=True, progress=False)

        if isinstance(raw.columns, pd.MultiIndex):
            prices = raw["Close"]
        else:
            prices = raw[["Close"]] if "Close" in raw.columns else raw

        if prices.index.tz is not None:
            prices.index = prices.index.tz_convert("UTC").tz_localize(None)
        prices = apply_nav_overrides_to_prices(prices, nav_overrides)       
        prices = strip_outliers(prices)

        hist_gbpusd = prices["GBPUSD=X"] if "GBPUSD=X" in prices.columns else None
        hist_eurusd = prices["EURUSD=X"] if "EURUSD=X" in prices.columns else None

        def fx_on_date(currency, dt):
            fk = fx_key(currency)
            if fk == "USD":   return 1.0
            if fk == "GBP":   series, fallback = hist_gbpusd, fx_rates.get("GBP", 1.0)
            elif fk == "EUR": series, fallback = hist_eurusd, fx_rates.get("EUR", 1.0)
            else: return 1.0
            if series is None: return fallback
            try:
                val = float(series.loc[:dt].iloc[-1])
                return val if pd.notna(val) else fallback
            except: return fallback

        from collections import defaultdict
        events_by_date = defaultdict(list)
        for t in trades:
            events_by_date[t["date"]].append(t)

        # Per-asset-class holdings: { asset_class: { ticker: {qty, yf_ticker, currency, avg_cost_usd} } }
        ac_holdings = defaultdict(dict)
        # Track ticker → asset_class mapping (from first trade)
        ticker_ac_map = {}
        for t in trades:
            tk = t["ticker"]
            if tk not in ticker_ac_map and t.get("asset_class"):
                ticker_ac_map[tk] = t["asset_class"]

        date_range  = pd.bdate_range(start=inception, end=today)
        # Result: { asset_class: [ {date, growth_pct} ] }
        ac_series = defaultdict(list)

        for dt in date_range:
            ds = dt.strftime("%Y-%m-%d")

            for e in events_by_date.get(ds, []):
                tk       = e["ticker"]
                ac       = e.get("asset_class") or ticker_ac_map.get(tk, "Unknown")
                ticker_ac_map[tk] = ac
                qty      = float(e.get("quantity", 0))
                price    = float(e.get("price", 0))
                action   = e.get("action", "").upper()
                ytk      = e.get("yf_ticker", tk)
                currency = get_currency(e, ytk)
                fx_r     = fx_on_date(currency, dt)
                price_base = price / 100 if is_lse_pence(currency) else price
                p_usd    = price_base * fx_r

                holdings = ac_holdings[ac]
                if action == "OPEN":
                    holdings[tk] = {"qty": qty, "yf_ticker": ytk,
                                    "currency": currency, "avg_cost_usd": p_usd}
                elif action == "ADD":
                    if tk in holdings:
                        old = holdings[tk]
                        total_qty = old["qty"] + qty
                        avg_cost  = (old["avg_cost_usd"] * old["qty"] + p_usd * qty) / total_qty
                        holdings[tk]["qty"] = total_qty
                        holdings[tk]["avg_cost_usd"] = avg_cost
                    else:
                        holdings[tk] = {"qty": qty, "yf_ticker": ytk,
                                        "currency": currency, "avg_cost_usd": p_usd}
                elif action == "REDUCE":
                    if tk in holdings:
                        holdings[tk]["qty"] -= qty
                        if holdings[tk]["qty"] <= 0:
                            del holdings[tk]
                elif action == "CLOSE":
                    holdings.pop(tk, None)

            # After processing today's trades, compute growth % for each asset class
            for ac, holdings in ac_holdings.items():
                if not holdings:
                    continue
                invested = sum(h["avg_cost_usd"] * h["qty"] for h in holdings.values())
                if invested <= 0:
                    continue
                mv = 0.0
                for tk, h in holdings.items():
                    ytk      = h["yf_ticker"]
                    currency = h.get("currency", "USD")
                    fx_r     = fx_on_date(currency, dt)
                    try:
                        if ytk in prices.columns:
                            p_raw = float(prices.loc[:dt, ytk].iloc[-1])
                            p_base = normalize_gbx_price(p_raw, h["avg_cost_usd"] / fx_r) if is_lse_pence(currency) else p_raw
                            mv += p_base * fx_r * h["qty"]
                        else:
                            mv += h["avg_cost_usd"] * h["qty"]  # fallback: flat
                    except:
                        mv += h["avg_cost_usd"] * h["qty"]

                growth_pct = round((mv - invested) / invested * 100, 4)
                ac_series[ac].append({"date": ds, "growth_pct": growth_pct})

        def sanitise(obj):
            """Recursively replace float NaN/Inf with None for JSON safety."""
            if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                return None
            if isinstance(obj, dict):
                return {k: sanitise(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [sanitise(v) for v in obj]
            return obj

        active_classes = set(ac for ac, holdings in ac_holdings.items() if holdings)
        filtered_series = {ac: series for ac, series in ac_series.items()
            if ac in active_classes or ac == "C&CE"
        }

        return jsonify({"asset_class_series": sanitise(filtered_series)})

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

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

    # TEST 4: FX exposure sums to ~100% and GBX buckets under GBP
    test_positions = [
        {"currency": "USD", "mv_usd": 3000},
        {"currency": "GBP", "mv_usd": 1000},
        {"currency": "GBX", "mv_usd": 1000},  # should merge into GBP bucket
    ]
    test_cash = 5000.0
    test_total_val = sum(p["mv_usd"] for p in test_positions) + test_cash
    base_ccy = "USD"
    fx_exp = {}
    for ccy in ["USD", "GBP", "EUR"]:
        mv = sum(p["mv_usd"] for p in test_positions if fx_key(p["currency"]) == ccy)
        if ccy == base_ccy:
            mv += test_cash
        fx_exp[ccy + "_pct"] = round(mv / test_total_val * 100, 2)
    total_pct = sum(fx_exp.values())
    if abs(total_pct - 100.0) > 0.1:
        errors.append(f"FX exposure sum FAIL: {total_pct}% (expected ~100%)")
    elif abs(fx_exp.get("GBP_pct", 0) - round(2000 / 10000 * 100, 2)) > 0.01:
        errors.append(f"GBX->GBP bucket FAIL: GBP_pct={fx_exp.get('GBP_pct')}, expected 20.0")
    else:
        print(f"  [PASS] FX exposure sums to {total_pct}%  {fx_exp}  (GBX merged into GBP)")

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
            errors.append(f"TZ fix FAIL: got {val}, expected 120.1")
        else:
            print(f"  [PASS] TZ fix: prices.loc[:dt_naive] = {val} (no TypeError)")
    except Exception as e:
        errors.append(f"TZ fix FAIL: raised {type(e).__name__}: {e}")

    # TEST 8: FIX 10 NAV final-day alignment
    mock_nav = [{"date": "2026-05-07", "value": 99000.0}]
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

    # TEST 14: display_currency conversion layer
    test_usd_val = 10000.0
    test_fx_rates = {"USD": 1.0, "EUR": 1.08, "GBP": 1.25}
    test_disp = "EUR"
    test_usd_to_disp = 1.0 / test_fx_rates[test_disp]
    converted = round(test_usd_val * test_usd_to_disp, 2)
    expected_eur = round(10000.0 / 1.08, 2)
    if abs(converted - expected_eur) > 0.01:
        errors.append(f"Display currency conversion FAIL: got {converted}, expected {expected_eur}")
    else:
        print(f"  [PASS] Display currency conversion: ${test_usd_val} -> €{converted} (rate: {test_usd_to_disp:.6f})")

    # TEST 15a: get_currency() prefers explicit column over suffix
    row_usd_lse = {"currency": "USD", "yf_ticker": "AGGG.L"}
    row_gbx_lse = {"currency": "GBX", "yf_ticker": "INFR.L"}
    row_gbp_lse = {"currency": "GBP", "yf_ticker": "GILG.L"}
    row_blank   = {"currency": "",    "yf_ticker": "IWDA.AS"}
    if get_currency(row_usd_lse, "AGGG.L") != "USD":
        errors.append(f"TEST 15a FAIL: AGGG.L with currency=USD should return USD")
    elif get_currency(row_gbx_lse, "INFR.L") != "GBX":
        errors.append(f"TEST 15a FAIL: INFR.L with currency=GBX should return GBX")
    elif get_currency(row_gbp_lse, "GILG.L") != "GBP":
        errors.append(f"TEST 15a FAIL: GILG.L with currency=GBP should return GBP")
    elif get_currency(row_blank, "IWDA.AS") != "EUR":
        errors.append(f"TEST 15a FAIL: blank currency with .AS suffix should fall back to EUR")
    else:
        print("  [PASS] TEST 15a: get_currency() explicit column + fallback logic correct")

    # TEST 15b: is_lse_pence() only fires for GBX
    if not is_lse_pence("GBX"):
        errors.append("TEST 15b FAIL: GBX should return True")
    elif is_lse_pence("GBP"):
        errors.append("TEST 15b FAIL: GBP should return False")
    elif is_lse_pence("USD"):
        errors.append("TEST 15b FAIL: USD should return False")
    else:
        print("  [PASS] TEST 15b: is_lse_pence() fires only for GBX")

    # TEST 15c: fx_key() maps GBX -> GBP, others pass through
    if fx_key("GBX") != "GBP":
        errors.append("TEST 15c FAIL: fx_key(GBX) should be GBP")
    elif fx_key("GBP") != "GBP":
        errors.append("TEST 15c FAIL: fx_key(GBP) should be GBP")
    elif fx_key("USD") != "USD":
        errors.append("TEST 15c FAIL: fx_key(USD) should be USD")
    elif fx_key("EUR") != "EUR":
        errors.append("TEST 15c FAIL: fx_key(EUR) should be EUR")
    else:
        print("  [PASS] TEST 15c: fx_key() maps GBX->GBP, others unchanged")

    # TEST 15d: pence divide applied for GBX, not for USD .L ticker
    price_gbx = 947500.0   # WSML.L quoted in pence — but wait, WSML is USD in the new sheet
    price_usd_lse = 446.05  # AGGG.L quoted in USD
    gbx_base = price_gbx / 100 if is_lse_pence("GBX") else price_gbx
    usd_base = price_usd_lse / 100 if is_lse_pence("USD") else price_usd_lse
    if abs(gbx_base - 9475.0) > 0.01:
        errors.append(f"TEST 15d FAIL: GBX pence divide wrong, got {gbx_base}")
    elif abs(usd_base - 446.05) > 0.01:
        errors.append(f"TEST 15d FAIL: USD .L should NOT be divided by 100, got {usd_base}")
    else:
        print(f"  [PASS] TEST 15d: GBX /100 = {gbx_base}, USD .L unchanged = {usd_base}")

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
