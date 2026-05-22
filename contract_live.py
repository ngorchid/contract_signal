"""
Live execution of the government-contract options strategy.

Run ~30 min before market close (e.g. via cron at 15:30 ET).
Checks USASpending for today's new awards, applies 8-K and materiality
filters, buys 30% OTM ~1-year calls for qualifying names, and sells any
positions that have reached their hold-day target.  Sends an HTML email
summary at the end.
"""

import json
import logging
import os
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from ib_insync import MarketOrder, Option, Stock

from contract_strategy import (
    bs_call_price,
    build_8k_map,
    enrich_with_tickers,
    fetch_new_contracts,
    fetch_shares_outstanding,
    fetch_ticker_cik_map,
    filter_preannounced,
    historical_vol,
)

POSITIONS_FILE        = Path("contract_positions.json")
TRADE_LOG_FILE        = Path("contract_trade_log.json")
PROCESSED_IDS_FILE    = Path("contract_processed_ids.json")
UNKNOWN_FILE          = Path("contract_unknown_recipients.json")
RISK_FREE = 0.05
OPTION_EXPIRY_YEARS = 1.0


# ── Position store ────────────────────────────────────────────────────────────

def load_option_positions():
    if POSITIONS_FILE.exists():
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    return []


def load_trade_log():
    if TRADE_LOG_FILE.exists():
        with open(TRADE_LOG_FILE) as f:
            return json.load(f)
    return []


def load_processed_ids():
    if PROCESSED_IDS_FILE.exists():
        with open(PROCESSED_IDS_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids):
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(ids), f, indent=2)


def append_trade_log(sell_record):
    log = load_trade_log()
    log.append(sell_record)
    with open(TRADE_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


def total_pnl_since_inception():
    return sum(t.get("pnl", 0) for t in load_trade_log())


def load_unknown_recipients():
    if UNKNOWN_FILE.exists():
        with open(UNKNOWN_FILE) as f:
            return json.load(f)
    return {}


def save_unknown_recipients(data):
    with open(UNKNOWN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def lookup_unknown_recipients(unmatched_rows):
    """
    For each unmatched recipient, try yfinance search to find a candidate ticker.
    Merges with any previously seen unknowns and returns only new ones found this run.
    unmatched_rows: list of dicts with keys 'recipient', 'amount', 'date'
    """
    known = load_unknown_recipients()
    new_candidates = []

    for row in unmatched_rows:
        name = row["recipient"]
        if name in known:
            continue  # already logged previously

        candidate_ticker = None
        candidate_name = None
        try:
            results = yf.Search(name, max_results=1).quotes
            if results:
                hit = results[0]
                candidate_ticker = hit.get("symbol")
                candidate_name = hit.get("shortname") or hit.get("longname")
        except Exception:
            pass

        entry = {
            "recipient": name,
            "amount": row["amount"],
            "first_seen": row["date"],
            "suggested_ticker": candidate_ticker,
            "suggested_name": candidate_name,
            "reviewed": False,
        }
        known[name] = entry
        new_candidates.append(entry)
        logging.info(
            f"[UNKNOWN] {name} — suggested ticker: {candidate_ticker} ({candidate_name})"
        )

    save_unknown_recipients(known)
    return new_candidates


def save_option_positions(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2, default=str)


# ── IB helpers ────────────────────────────────────────────────────────────────

def get_stock_price(ib, symbol, currency="USD"):
    contract = Stock(symbol, "SMART", currency)
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=1,
        keepUpToDate=False,
    )
    if not bars:
        return None
    return bars[-1].close


def get_option_params(ib, symbol, currency="USD"):
    """Return (sorted strikes list, sorted expirations list in YYYYMMDD) for a stock."""
    stock = Stock(symbol, "SMART", currency)
    ib.qualifyContracts(stock)
    params = ib.reqSecDefOptParams(symbol, "", "STK", stock.conId)
    if not params:
        return None, None
    p = params[0]
    return sorted(p.strikes), sorted(p.expirations)


def nearest_strike_above(strikes, target):
    above = [s for s in strikes if s >= target]
    return min(above) if above else max(strikes)


def nearest_expiry_after(expirations, target_date):
    target_str = target_date.strftime("%Y%m%d")
    after = [e for e in expirations if e >= target_str]
    return min(after) if after else max(expirations)


def qualify_option(ib, symbol, strike, expiry_str, currency="USD"):
    opt = Option(symbol, expiry_str, strike, "C", "SMART", currency=currency)
    qualified = ib.qualifyContracts(opt)
    return qualified[0] if qualified else None


def get_option_mid(ib, opt_contract):
    ticker = ib.reqMktData(opt_contract, "", True, False)
    ib.sleep(2)
    price = ticker.midpoint()
    if not price or np.isnan(price):
        price = ticker.last
    if not price or np.isnan(price):
        price = ticker.close
    ib.cancelMktData(opt_contract)
    return price


# ── Materiality for today's events ───────────────────────────────────────────

def compute_live_events(contracts_df, min_materiality, max_market_cap):
    """
    Given a DataFrame of today's contracts (already ticker-enriched),
    fetch recent prices, compute materiality, and apply filters.
    Returns a filtered DataFrame ready for signal generation.
    """
    if contracts_df.empty:
        return pd.DataFrame()

    tickers = contracts_df["ticker"].unique().tolist()
    today = datetime.now().date()
    start = (pd.to_datetime(today) - timedelta(days=200)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    raw = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        return pd.DataFrame()
    prices = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].rename(columns={"Close": tickers[0]})

    shares_map = fetch_shares_outstanding(tickers)

    records = []
    for _, row in contracts_df.iterrows():
        ticker = row["ticker"]
        if ticker not in prices.columns:
            continue
        series = prices[ticker].dropna()
        if series.empty:
            continue
        entry_price = float(series.iloc[-1])
        shares = shares_map.get(ticker, np.nan)
        if shares and not np.isnan(shares) and entry_price > 0:
            market_cap = shares * entry_price
            materiality = row["Transaction Amount"] / market_cap
        else:
            market_cap = np.nan
            materiality = np.nan

        # Historical vol for option pricing later (informational only here)
        sigma = historical_vol(series, window=min(60, len(series) - 1)) if len(series) >= 10 else np.nan

        records.append({
            "ticker": ticker,
            "award_id": row.get("Award ID", ""),
            "recipient": row["Recipient Name"],
            "action_date": row["Action Date"],
            "amount": row["Transaction Amount"],
            "agency": row.get("Awarding Agency", ""),
            "materiality": materiality,
            "market_cap": market_cap,
            "entry_price": entry_price,
            "sigma": sigma,
        })

    df = pd.DataFrame(records)
    if df.empty:
        return df

    if min_materiality is not None:
        df = df[df["materiality"] >= min_materiality]
    if max_market_cap is not None:
        df = df[df["market_cap"] <= max_market_cap]

    return df.dropna(subset=["materiality"])


# ── Email ─────────────────────────────────────────────────────────────────────

def _table(headers, rows):
    th = "".join(f"<th style='padding:6px 12px;border:1px solid #ccc;background:#f0f0f0'>{h}</th>" for h in headers)
    body_html = ""
    for row in rows:
        td = "".join(f"<td style='padding:6px 12px;border:1px solid #ccc'>{v}</td>" for v in row)
        body_html += f"<tr>{td}</tr>"
    return f"<table style='border-collapse:collapse;font-family:monospace;font-size:13px'><tr>{th}</tr>{body_html}</table>"


def build_email_body(buys, sells, today_str, new_unknowns=None):
    total_pnl = total_pnl_since_inception()
    color = "green" if total_pnl >= 0 else "red"
    sections = [
        f"<h2>Contract Signal Report — {today_str}</h2>",
        f"<p><strong>Cumulative P&amp;L since inception:</strong> "
        f"<span style='color:{color};font-size:16px'>${total_pnl:+,.2f}</span></p>",
    ]

    sections.append(f"<h3>Positions Opened ({len(buys)})</h3>")
    if buys:
        rows = [
            (
                b["ticker"],
                b["recipient"][:40],
                f"${b['amount']/1e6:.0f}M",
                f"${b['strike']:.2f}",
                b["expiry"],
                f"${b['entry_price']:.2f}",
                b["quantity"],
                b["exit_date"],
            )
            for b in buys
        ]
        sections.append(_table(
            ["Ticker", "Recipient", "Award", "Strike", "Expiry", "Premium", "Contracts", "Target Exit"],
            rows,
        ))
    else:
        sections.append("<p>No positions opened today.</p>")

    sections.append(f"<h3>Positions Closed ({len(sells)})</h3>")
    if sells:
        rows = [
            (
                s["ticker"],
                s["recipient"][:40],
                f"${s['strike']:.2f}",
                s["expiry"],
                f"${s['entry_price']:.2f}",
                f"${s.get('exit_price', 0):.2f}",
                s["quantity"],
                f"${s.get('pnl', 0):+.2f}",
                s["entry_date"],
                s["exit_date"],
            )
            for s in sells
        ]
        sections.append(_table(
            ["Ticker", "Recipient", "Strike", "Expiry", "Entry", "Exit", "Contracts", "P&L", "Open Date", "Close Date"],
            rows,
        ))
    else:
        sections.append("<p>No positions closed today.</p>")

    if new_unknowns:
        sections.append(
            "<h3 style='color:#b8600a'>⚠ New Unmapped Recipients — Review Needed</h3>"
            "<p>These companies had awards ≥ $50M but no ticker in the map. "
            "If they are publicly traded, add them to <code>CONTRACTOR_TICKER_MAP</code> in "
            "<code>contract_strategy.py</code>.</p>"
        )
        rows = [
            (
                u["recipient"][:50],
                f"${u['amount']/1e6:.0f}M",
                u["first_seen"],
                u.get("suggested_ticker") or "—",
                (u.get("suggested_name") or "—")[:40],
            )
            for u in new_unknowns
        ]
        sections.append(_table(
            ["Recipient", "Award", "Date", "Suggested Ticker", "Suggested Name"],
            rows,
        ))

    return "\n".join(sections)


def send_daily_report(buys, sells, today_str, new_unknowns=None):
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASS")
    to_email = os.getenv("TO_EMAIL")
    if not all([email_user, email_pass, to_email]):
        logging.warning("Email credentials not set — skipping report.")
        return

    subject = f"Contract Signal — {today_str}: {len(buys)} bought, {len(sells)} sold"
    body = build_email_body(buys, sells, today_str, new_unknowns=new_unknowns)

    msg = MIMEMultipart("alternative")
    msg["From"] = email_user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(email_user, email_pass)
            server.send_message(msg)
        logging.info(f"Daily report sent to {to_email}")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")


# ── Core ──────────────────────────────────────────────────────────────────────

def run_contract_live(ib, config, dry_run=False):
    """
    Run once ~30 min before market close.
    Sells positions past their hold period, then buys new signals.
    Sends an email summary.
    dry_run=True logs all actions but places no orders and writes no files.
    """
    if dry_run:
        logging.info("=== DRY RUN MODE — no orders will be placed ===")

    cfg = config["contract_strategy"]
    hold_days            = cfg["hold_days"]
    min_amount           = cfg["min_amount"]
    lookback_days        = cfg.get("8k_lookback_days", 28)
    max_market_cap       = cfg.get("max_market_cap")
    call_otm_pct         = cfg.get("call_otm_pct", 0.30)
    contracts_per_signal = cfg.get("contracts_per_signal", 1)
    min_materiality      = cfg.get("min_materiality_live")
    reporting_lookback   = cfg.get("reporting_lookback_days", 3)
    max_reporting_lag    = cfg.get("max_reporting_lag_days", 5)

    today = datetime.now().date()
    today_str = str(today)

    buys = []
    sells = []
    positions = load_option_positions()
    remaining = []

    # ── Sell positions past hold period ──────────────────────────────────────
    for pos in positions:
        exit_date = pd.to_datetime(pos["exit_date"]).date()
        if today < exit_date:
            remaining.append(pos)
            continue

        ticker = pos["ticker"]
        try:
            opt_contract = qualify_option(ib, ticker, pos["strike"], pos["expiry"])
            if opt_contract is None:
                logging.warning(f"[{ticker}] Could not qualify option for sell — keeping position.")
                remaining.append(pos)
                continue

            exit_price = get_option_mid(ib, opt_contract)
            if not exit_price:
                logging.warning(f"[{ticker}] No exit price — keeping position.")
                remaining.append(pos)
                continue

            pnl = (exit_price - pos["entry_price"]) * pos["quantity"] * 100
            sell_record = {**pos, "exit_price": exit_price, "pnl": pnl, "exit_date_actual": today_str}
            if dry_run:
                logging.info(f"[DRY RUN] Would sell {ticker} call K={pos['strike']} exp={pos['expiry']} @ {exit_price:.2f}  PnL=${pnl:+.2f}")
            else:
                order = MarketOrder("SELL", pos["quantity"])
                ib.placeOrder(opt_contract, order)
                ib.sleep(2)
                append_trade_log(sell_record)
                logging.info(f"[{ticker}] Sold call K={pos['strike']} exp={pos['expiry']} @ {exit_price:.2f}  PnL=${pnl:+.2f}")
            sells.append(sell_record)

        except Exception as e:
            logging.error(f"[{ticker}] Sell failed: {e}")
            remaining.append(pos)

    # ── Fetch recent contracts (lookback window to handle reporting lag) ─────
    fetch_start = (pd.to_datetime(today) - timedelta(days=reporting_lookback)).strftime("%Y-%m-%d")
    logging.info(f"Fetching contracts {fetch_start} to {today_str} (reporting_lookback={reporting_lookback}d)...")
    contracts_df = fetch_new_contracts(fetch_start, today_str, min_amount=min_amount)

    # Drop contracts already processed in a previous run
    processed_ids = load_processed_ids()
    if not contracts_df.empty and "Award ID" in contracts_df.columns:
        before = len(contracts_df)
        contracts_df = contracts_df[~contracts_df["Award ID"].isin(processed_ids)]
        logging.info(f"Filtered {before - len(contracts_df)} already-processed awards; {len(contracts_df)} new.")

    # Drop contracts whose action date is too stale to be worth entering
    if not contracts_df.empty:
        contracts_df = contracts_df[
            (pd.to_datetime(today) - contracts_df["Action Date"]).dt.days <= max_reporting_lag
        ]
        logging.info(f"{len(contracts_df)} contracts within max_reporting_lag={max_reporting_lag}d.")

    new_unknowns = []
    if contracts_df.empty:
        logging.info("No new contracts in lookback window — nothing to buy.")
    else:
        # Detect unmapped recipients before filtering them out
        from contract_strategy import map_to_ticker
        unmatched = contracts_df[contracts_df["Recipient Name"].apply(map_to_ticker).isna()]
        if not unmatched.empty:
            unmatched_rows = [
                {"recipient": r["Recipient Name"], "amount": r["Award Amount"], "date": str(r["Action Date"])[:10]}
                for _, r in unmatched.iterrows()
            ]
            new_unknowns = lookup_unknown_recipients(unmatched_rows)

        contracts_df = enrich_with_tickers(contracts_df)

        if not contracts_df.empty:
            tickers = contracts_df["ticker"].unique().tolist()

            # 8-K filter — filter_preannounced expects lowercase 'action_date'
            ticker_cik_map = fetch_ticker_cik_map()
            filing_dates_map = build_8k_map(tickers, ticker_cik_map)
            contracts_df = contracts_df.rename(columns={"Action Date": "action_date"})
            contracts_df = filter_preannounced(contracts_df, filing_dates_map, lookback_days=lookback_days)
            # compute_live_events expects 'Action Date' (original casing)
            contracts_df = contracts_df.rename(columns={"action_date": "Action Date"})

            events_df = compute_live_events(contracts_df, min_materiality, max_market_cap)

            if events_df.empty:
                logging.info("No qualifying contracts after filters.")
            else:
                logging.info(f"{len(events_df)} qualifying contract(s) — placing buy orders.")

                # Deduplicate: one position per ticker (take highest materiality if multiple)
                events_df = events_df.sort_values("materiality", ascending=False).drop_duplicates("ticker")

                for _, ev in events_df.iterrows():
                    ticker = ev["ticker"]
                    try:
                        current_price = get_stock_price(ib, ticker)
                        if not current_price:
                            logging.warning(f"[{ticker}] No price — skipping.")
                            continue

                        strikes, expirations = get_option_params(ib, ticker)
                        if not strikes or not expirations:
                            logging.warning(f"[{ticker}] No option params — skipping.")
                            continue

                        target_strike = current_price * (1 + call_otm_pct)
                        strike = nearest_strike_above(strikes, target_strike)

                        target_expiry = pd.to_datetime(today) + timedelta(days=365)
                        expiry_str = nearest_expiry_after(expirations, target_expiry)

                        opt_contract = qualify_option(ib, ticker, strike, expiry_str)
                        if opt_contract is None:
                            logging.warning(f"[{ticker}] Could not qualify option — skipping.")
                            continue

                        entry_price = get_option_mid(ib, opt_contract)
                        if not entry_price:
                            logging.warning(f"[{ticker}] No option price — skipping.")
                            continue

                        exit_date = pd.bdate_range(start=today, periods=hold_days + 1)[-1].date()

                        pos = {
                            "ticker": ticker,
                            "recipient": ev["recipient"],
                            "strike": strike,
                            "expiry": expiry_str,
                            "quantity": contracts_per_signal,
                            "entry_price": entry_price,
                            "entry_date": today_str,
                            "exit_date": str(exit_date),
                            "amount": float(ev["amount"]),
                            "materiality": float(ev["materiality"]),
                        }
                        award_id = ev.get("award_id", "")
                        if dry_run:
                            logging.info(
                                f"[DRY RUN] Would buy {contracts_per_signal}x {ticker} call "
                                f"K={strike} exp={expiry_str} @ {entry_price:.2f}  "
                                f"(exit target: {exit_date})"
                            )
                        else:
                            order = MarketOrder("BUY", contracts_per_signal)
                            ib.placeOrder(opt_contract, order)
                            ib.sleep(2)
                            remaining.append(pos)
                            logging.info(
                                f"[{ticker}] Bought {contracts_per_signal}x call "
                                f"K={strike} exp={expiry_str} @ {entry_price:.2f}"
                            )
                        # Mark as processed regardless of dry_run so we don't re-evaluate tomorrow
                        if award_id:
                            processed_ids.add(award_id)
                        buys.append(pos)
                    except Exception as e:
                        logging.error(f"[{ticker}] Buy failed: {e}")

    if not dry_run:
        save_option_positions(remaining)
        save_processed_ids(processed_ids)
    send_daily_report(buys, sells, today_str, new_unknowns=new_unknowns)

    logging.info(f"Contract live run complete: {len(buys)} bought, {len(sells)} sold.")
    return buys, sells
