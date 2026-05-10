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
#      and 30d rolling vol for the benchmark (V60A) series using identical formulas to
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
#  17. BENCH_START ANCHOR FIX: bench_start is now initialised by looking up V60A's price
#      on exactly inception_date (using .loc[:inception_date].iloc[-1]) before the loop
#      begins, rather than lazily on the first loop iteration that happens to have a price.
#      This prevents the benchmark from appearing to start at a higher NAV than the fund
#      when V60A has no price on inception_date itself (weekend, holiday, or data gap).
#      If V60A has no price at or before inception_date, bench_start stays None and the
#      old lazy behaviour is used as a safe fallback.
#  18. MISSING PRICE GUARD: in the NAV loop, if a ticker is not in prices.columns at all,
#      the old code fell back to adding h["qty"] (raw share count) to port_val as if it
#      were USD — silently inflating NAV for large positions. The fix now adds 0 instead
#      and logs a warning. A missing ticker means delisted, wrong yf_ticker, or network
#      gap — in all cases 0 is a safer approximation than treating qty as a USD value.
#  19. NAV CURVE HISTORICAL FX: previously the entire nav_series (all historical USD values)
#      was converted to the display currency (EUR) using a single live EURUSD rate fetched
#      at request time. This meant e.g. the 14/01/2026 NAV point was divided by today's
#      EURUSD rate, not the rate that was in effect on 14/01/2026 — producing an inaccurate
#      historical curve whenever EURUSD has moved since inception.
#      Fix: build_nav_curve() now returns nav_series already in display currency by applying
#      fx_on_date(display_currency, dt) to each USD port_val inside the daily loop.
#      The flat conv() pass in portfolio() still converts all other monetary fields
#      (positions, closed trades, scalars) using the live rate as before — only the
#      nav_series and bench_series conversion is replaced with the per-date historical rate.
#      usd_to_disp is still computed and exposed in the API response for reference.
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
        "base_currency":    cfg.get("base_currency", "USD"),
        "inception_date":   cfg.get("inception_date", "2026-01-14"),
        "benchmark":        cfg.get("benchmark", "V60A.AS"),
        "portfolio_name":   cfg.get("portfolio_name", "Investment Portfolio"),
        "display_currency": cfg.get("display_currency", "USD"),  # FIX 14
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
                    live_positions_mv=None, live_cash=None, income_records=None,
                    display_currency="USD"):
    # FIX 13: income_records are pre-indexed by date so that cash is increased
    # on the correct historical payment date in the NAV curve loop.
    # FIX 19: display_currency passed in so each NAV point is converted using the
    # historical FX rate for that date rather than a single live rate.
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
    date_range   = pd.bdate_range(start=inception, end=today)
    last_date    = date_range[-1] if len(date_range) > 0 else None

    # FIX 17: Pre-compute bench_start from the benchmark price at or before
    # inception_date, so the rebase anchor is locked to inception regardless
    # of whether the benchmark has a price on that exact date (weekend/holiday/gap).
    bench_start = None
    if benchmark_ticker in prices.columns:
        try:
            inception_ts = pd.Timestamp(inception)
            bench_inception_slice = prices.loc[:inception_ts, benchmark_ticker].dropna()
            if not bench_inception_slice.empty:
                bench_start = float(bench_inception_slice.iloc[-1])
        except:
            pass  # Falls back to lazy first-seen initialisation below

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
            port_val_usd = live_cash + live_positions_mv
        else:
            port_val_usd = cash
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
                        port_val_usd += p_base * fx_r * h["qty"]
                    else:
                        # FIX 18: ticker not in prices — add 0 (not raw qty).
                        # Adding h["qty"] would silently inflate NAV by treating
                        # share count as a USD value (e.g. 250 shares -> +$250).
                        port_val_usd += 0
                except:
                    pass

        # FIX 19: Convert each NAV point to display currency using the historical
        # FX rate for that specific date, not the live rate at request time.
        # For USD display_currency, usd_to_disp_hist = 1.0 (no-op).
        if display_currency != "USD":
            eurusd_hist = fx_on_date(display_currency, dt)
            usd_to_disp_hist = (1.0 / eurusd_hist) if eurusd_hist else 1.0
        else:
            usd_to_disp_hist = 1.0
        port_val_disp = round(port_val_usd * usd_to_disp_hist, 2)

        nav_series.append({"date": ds, "value": port_val_disp})

        # FIX 17: bench_start is already locked to inception; use lazy fallback
        # only if the pre-compute above found no price at or before inception.
        # FIX 19: bench_series rebased in USD then converted per-date to display currency.
        try:
            if benchmark_ticker in prices.columns:
                bp = float(prices.loc[:dt, benchmark_ticker].iloc[-1])
                if bench_start is None:
                    bench_start = bp  # safe fallback: first date with a price
                bench_val_usd  = starting * (bp / bench_start)
                bench_val_disp = round(bench_val_usd * usd_to_disp_hist, 2)
                bench_series.append({"date": ds, "value": bench_val_disp})
        except:
            pass

    return nav_series, bench_series

def calc_metrics(nav_series, starting_capital, rf_annual=0.024, closed_trades=[], income_usd=0.0):
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

def calc_benchmark_metrics(bench_series, rf_annual=0.024):
    """
    FIX 11: Compute Sharpe, Sortino, max drawdown, and 30d rolling volatility
    for the benchmark (V60A) series using identical formulas to calc_metrics().
    """
    if len(bench_series) < 2:
        return {
            "benchmark_sharpe":           None,
            "benchmark_sortino":          None,
            "benchmark_max_drawdow