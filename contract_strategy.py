import os
import pandas as pd
import numpy as np
import requests
import yfinance as yf
import logging
import time
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from pathlib import Path
from scipy.stats import norm
from dotenv import load_dotenv

load_dotenv()

USASPENDING_API = "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
CACHE_DIR = Path("contract_cache")
EDGAR_CACHE_DIR = Path("edgar_cache")
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC requires a User-Agent with contact info — set EDGAR_CONTACT_EMAIL in .env
EDGAR_HEADERS = {"User-Agent": f"trading-research {os.environ.get('EDGAR_CONTACT_EMAIL', 'your-email@example.com')}"}


def fetch_new_contracts(start_date, end_date, min_amount=50_000_000, page_limit=10):
    """
    Pull new contract awards (Mod=0) from USASpending.gov for a date range.
    Returns a DataFrame with one row per new award.
    """
    results = []
    page = 1

    while page <= page_limit:
        payload = {
            "filters": {
                "award_type_codes": ["A", "B", "C", "D"],
                "time_period": [{"start_date": start_date, "end_date": end_date}],
                "award_amounts": [{"lower_bound": min_amount}]
            },
            "fields": [
                "Award ID", "Recipient Name", "Recipient UEI",
                "Transaction Amount", "Action Date", "Action Type", "Mod",
                "Awarding Agency", "Transaction Description", "naics_description"
            ],
            "page": page,
            "limit": 100,
            "sort": "Transaction Amount",
            "order": "desc"
        }

        try:
            resp = requests.post(USASPENDING_API, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.warning(f"API request failed (page {page}): {e}")
            break

        batch = data.get("results", [])
        results.extend(batch)

        if not data.get("page_metadata", {}).get("hasNext", False):
            break
        page += 1
        time.sleep(0.5)  # be polite to the API

    df = pd.DataFrame(results)
    if df.empty:
        return df

    # Keep only brand-new contracts, not modifications
    df = df[df["Mod"] == "0"].copy()
    df["Action Date"] = pd.to_datetime(df["Action Date"])
    df = df.sort_values("Action Date")
    return df


def fetch_contracts_range(start_date, end_date, min_amount=50_000_000, chunk_days=30):
    """
    Fetch contracts over a long date range by chunking into monthly batches
    and caching results to disk.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)
    all_dfs = []

    current = start
    while current < end:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        cache_file = CACHE_DIR / f"contracts_{current.date()}_{chunk_end.date()}.csv"

        if cache_file.exists():
            logging.info(f"Loading cached contracts {current.date()} – {chunk_end.date()}")
            try:
                df = pd.read_csv(cache_file, parse_dates=["Action Date"])
            except pd.errors.EmptyDataError:
                df = pd.DataFrame()
        else:
            logging.info(f"Fetching contracts {current.date()} – {chunk_end.date()}")
            df = fetch_new_contracts(
                current.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                min_amount=min_amount
            )
            df.to_csv(cache_file, index=False)

        if not df.empty:
            all_dfs.append(df)
        current = chunk_end + timedelta(days=1)

    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()


# Static mapping of publicly traded government contractors.
# Names matched case-insensitively via exact then partial match in map_to_ticker().
CONTRACTOR_TICKER_MAP = {
    # ── Prime defence primes ──────────────────────────────────────────────────
    "THE BOEING COMPANY": "BA",
    "BOEING": "BA",
    "LOCKHEED MARTIN CORPORATION": "LMT",
    "LOCKHEED MARTIN": "LMT",
    "RAYTHEON": "RTX",
    "RTX CORPORATION": "RTX",
    "RAYTHEON TECHNOLOGIES": "RTX",
    "GENERAL DYNAMICS CORPORATION": "GD",
    "GENERAL DYNAMICS": "GD",
    "NORTHROP GRUMMAN CORPORATION": "NOC",
    "NORTHROP GRUMMAN": "NOC",
    "L3HARRIS TECHNOLOGIES": "LHX",
    "L3HARRIS": "LHX",
    "BAE SYSTEMS": "BAESY",
    "HUNTINGTON INGALLS INDUSTRIES": "HII",
    "HUNTINGTON INGALLS": "HII",
    "LEIDOS HOLDINGS": "LDOS",
    "LEIDOS": "LDOS",
    "SCIENCE APPLICATIONS INTERNATIONAL": "SAIC",
    "SAIC": "SAIC",
    "BOOZ ALLEN HAMILTON": "BAH",
    "BOOZ ALLEN": "BAH",
    "ACCENTURE FEDERAL SERVICES": "ACN",
    "ACCENTURE": "ACN",
    "MANTECH INTERNATIONAL": "MANT",
    "MANTECH": "MANT",
    "CACI INTERNATIONAL": "CACI",
    "CACI": "CACI",
    "CACI INC": "CACI",
    "DXC TECHNOLOGY": "DXC",
    "KBR INC": "KBR",
    "KBR": "KBR",
    "VECTRUS": "AMTM",
    "TEXTRON": "TXT",
    "TEXTRON INC": "TXT",
    # ── Defence subsidiaries (map to parent) ─────────────────────────────────
    "ELECTRIC BOAT CORPORATION": "GD",
    "BATH IRON WORKS": "GD",
    "PRATT & WHITNEY": "RTX",
    "COLLINS AEROSPACE": "RTX",
    "SIKORSKY": "LMT",
    # ── Aerospace / drones / space ────────────────────────────────────────────
    "AEROVIRONMENT": "AVAV",
    "VIASAT": "VSAT",
    "PARSONS GOVERNMENT SERVICES": "PSN",
    "PARSONS CORPORATION": "PSN",
    "PARSONS": "PSN",
    "INTUITIVE MACHINES": "LUNR",
    # ── IT / tech ─────────────────────────────────────────────────────────────
    "GENERAL ELECTRIC": "GE",
    "HONEYWELL": "HON",
    "IBM": "IBM",
    "INTERNATIONAL BUSINESS MACHINES": "IBM",
    "MICROSOFT": "MSFT",
    "AMAZON": "AMZN",
    "AMAZON WEB SERVICES": "AMZN",
    "GOOGLE": "GOOGL",
    "ORACLE": "ORCL",
    "DELL": "DELL",
    "HP": "HPQ",
    "HEWLETT PACKARD": "HPQ",
    "CISCO SYSTEMS": "CSCO",
    "CISCO": "CSCO",
    "PALANTIR TECHNOLOGIES": "PLTR",
    "PALANTIR": "PLTR",
    "MOTOROLA SOLUTIONS": "MSI",
    "LUMEN TECHNOLOGIES": "LUMN",
    "GARTNER": "IT",
    "MANHATTAN ASSOCIATES": "MANH",
    "MAXIMUS FEDERAL": "MMS",
    "MAXIMUS": "MMS",
    # ── Engineering / construction ────────────────────────────────────────────
    "FLUOR": "FLR",
    "JACOBS": "J",
    "TETRA TECH": "TTEK",
    "EMCOR": "EME",
    "IRON MOUNTAIN": "IRM",
    "IRONMOUNTAIN": "IRM",
    # ── Industrial / defence vehicles ─────────────────────────────────────────
    "CUMMINS": "CMI",
    "GENERAL MOTORS": "GM",
    "ALLISON TRANSMISSION": "ALSN",
    "CRANE": "CR",
    # ── Healthcare / pharma / biodefence ─────────────────────────────────────
    "HUMANA": "HUM",
    "UNITED HEALTH": "UNH",
    "PFIZER": "PFE",
    "REGENERON PHARMACEUTICALS": "REGN",
    "REGENERON": "REGN",
    "MODERNATX": "MRNA",
    "BECTON DICKINSON": "BDX",
    "BECTON, DICKINSON": "BDX",
    "CARDINAL HEALTH": "CAH",
    "DAVITA": "DVA",
    "FRESENIUS MEDICAL CARE": "FMCQF",
    "EMERGENT BIOSOLUTIONS": "EBS",
    "CHARLES RIVER LABORATORIES": "CRL",
    # ── Government services / other ───────────────────────────────────────────
    "CORECIVIC": "CXW",
    "AMERESCO": "AMRC",
    "GLOBALFOUNDRIES": "GFS",
    # ── Private (explicitly excluded) ─────────────────────────────────────────
    "PERATON": None,
    "AMENTUM": None,
    "PAE INCORPORATED": None,
    "DYNCORP": None,
}


def fetch_ticker_cik_map():
    """Download the SEC's full ticker → CIK mapping."""
    resp = requests.get(EDGAR_TICKERS_URL, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}


def fetch_8k_dates_for_ticker(ticker, cik):
    """
    Fetch all 8-K filing dates for a company from SEC EDGAR.
    Results are cached to disk. Handles both recent and archived filings.
    """
    EDGAR_CACHE_DIR.mkdir(exist_ok=True)
    cache_file = EDGAR_CACHE_DIR / f"{ticker}_8k_dates.csv"

    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"])
        return df["date"].tolist()

    all_dates = []
    url = EDGAR_SUBMISSIONS_URL.format(cik=cik)

    try:
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        time.sleep(0.15)  # SEC rate limit: 10 req/sec
    except Exception as e:
        logging.warning(f"EDGAR fetch failed for {ticker}: {e}")
        return []

    def extract_8k_dates(filings_block):
        forms = filings_block.get("form", [])
        dates = filings_block.get("filingDate", [])
        items = filings_block.get("items", [""] * len(forms))
        return [
            pd.to_datetime(d)
            for f, d, it in zip(forms, dates, items)
            if f == "8-K" and "1.01" in str(it)
        ]

    all_dates.extend(extract_8k_dates(data.get("filings", {}).get("recent", {})))

    # Fetch older archived filing batches
    for file_entry in data.get("filings", {}).get("files", []):
        archive_url = f"https://data.sec.gov/submissions/{file_entry['name']}"
        try:
            r = requests.get(archive_url, headers=EDGAR_HEADERS, timeout=30)
            r.raise_for_status()
            all_dates.extend(extract_8k_dates(r.json()))
            time.sleep(0.15)
        except Exception as e:
            logging.warning(f"EDGAR archive fetch failed for {ticker}: {e}")

    pd.DataFrame({"date": all_dates}).to_csv(cache_file, index=False)
    return all_dates


def build_8k_map(tickers, ticker_cik_map):
    """Build a dict {ticker: [filing_date, ...]} for all tickers."""
    result = {}
    for ticker in tickers:
        cik = ticker_cik_map.get(ticker.upper())
        if not cik:
            logging.warning(f"No CIK found for {ticker}, skipping 8-K check")
            result[ticker] = []
            continue
        logging.info(f"Fetching 8-K dates for {ticker} (CIK {cik})")
        result[ticker] = fetch_8k_dates_for_ticker(ticker, cik)
    return result


def filter_preannounced(events_df, filing_dates_map, lookback_days=28):
    """
    Remove contract events where the company filed an 8-K within
    lookback_days before the action_date — indicating prior public disclosure.
    """
    def had_recent_8k(row):
        dates = filing_dates_map.get(row["ticker"], [])
        if not dates:
            return False
        action = pd.to_datetime(row["action_date"])
        cutoff = action - pd.Timedelta(days=lookback_days)
        return any(cutoff <= d <= action for d in dates)

    mask = ~events_df.apply(had_recent_8k, axis=1)
    n_removed = (~mask).sum()
    logging.info(f"8-K filter removed {n_removed} of {len(events_df)} events "
                 f"({n_removed/len(events_df)*100:.1f}%) with lookback={lookback_days}d")
    return events_df[mask].copy()


def map_to_ticker(name):
    """Map a contractor name to a ticker using the static map."""
    name_upper = name.upper().strip()
    if name_upper in CONTRACTOR_TICKER_MAP:
        return CONTRACTOR_TICKER_MAP[name_upper]
    for key, ticker in CONTRACTOR_TICKER_MAP.items():
        if key in name_upper:
            return ticker
    return None


def enrich_with_tickers(contracts_df):
    """Add ticker column and filter to publicly traded companies only."""
    contracts_df = contracts_df.copy()
    contracts_df["ticker"] = contracts_df["Recipient Name"].apply(map_to_ticker)
    # Drop private / unmapped companies
    contracts_df = contracts_df[contracts_df["ticker"].notna()].copy()
    return contracts_df


def download_price_data(tickers, start, end):
    """Download adjusted close prices for a list of tickers."""
    data = yf.download(tickers, start=start, end=end, auto_adjust=True)
    if isinstance(data.columns, pd.MultiIndex):
        return data["Close"]
    return data[["Close"]].rename(columns={"Close": tickers[0]})


def fetch_shares_outstanding(tickers):
    """Fetch shares outstanding for each ticker via yfinance. Returns dict {ticker: shares}."""
    shares = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            s = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
            shares[ticker] = s if s else np.nan
        except Exception:
            shares[ticker] = np.nan
    return shares


def bs_call_price(S, K, T, r, sigma):
    """Black-Scholes call price. Returns NaN if inputs are invalid."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return np.nan
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def historical_vol(prices_series, window=60):
    """Annualised historical volatility from log returns."""
    log_ret = np.log(prices_series / prices_series.shift(1)).dropna()
    return log_ret.rolling(window).std().iloc[-1] * np.sqrt(252)


def compute_forward_returns(contracts_df, prices, shares_outstanding, hold_days=5,
                            option_expiry_years=1.0, risk_free_rate=0.05):
    """
    For each contract award compute:
    - fwd_return: stock return over next hold_days trading days
    - materiality: award_amount / (shares_outstanding * price_at_award_date)
    """
    records = []

    for _, row in contracts_df.iterrows():
        ticker = row["ticker"]
        action_date = row["Action Date"]
        amount = row["Transaction Amount"]

        if ticker not in prices.columns:
            continue

        ticker_prices = prices[ticker].dropna()
        trading_days = ticker_prices.index

        future_days = trading_days[trading_days >= action_date]
        if len(future_days) < hold_days + 1:
            continue

        entry_date = future_days[0]
        exit_date = future_days[hold_days]

        entry_price = ticker_prices[entry_date]
        exit_price = ticker_prices[exit_date]
        fwd_return = (exit_price - entry_price) / entry_price

        # Materiality: award / market_cap at time of award
        # market_cap = shares_outstanding * price_at_award_date (shares treated as constant)
        shares = shares_outstanding.get(ticker, np.nan)
        if shares and not np.isnan(shares) and entry_price > 0:
            market_cap = shares * entry_price
            materiality = amount / market_cap
        else:
            materiality = np.nan

        # ATM call option return: buy 1yr call at entry, sell after hold_days
        # Use 60-day hist vol on prices up to (but not including) entry_date
        past_prices = ticker_prices[ticker_prices.index < entry_date].tail(120)
        n_past = len(past_prices)
        sigma = historical_vol(past_prices, window=min(60, n_past - 1)) if n_past >= 10 else np.nan
        T_entry = option_expiry_years
        T_exit = option_expiry_years - hold_days / 252.0
        K = entry_price  # ATM strike
        if sigma and not np.isnan(sigma):
            c_entry = bs_call_price(entry_price, K, T_entry, risk_free_rate, sigma)
            c_exit  = bs_call_price(exit_price,  K, T_exit,  risk_free_rate, sigma)
            option_return = (c_exit - c_entry) / c_entry if (c_entry and c_entry > 0) else np.nan

            # Single OTM call returns for several strike widths
            otm_call_returns = {}
            for otm_pct in [0.10, 0.20, 0.30]:
                K_otm = entry_price * (1 + otm_pct)
                co_entry = bs_call_price(entry_price, K_otm, T_entry, risk_free_rate, sigma)
                co_exit  = bs_call_price(exit_price,  K_otm, T_exit,  risk_free_rate, sigma)
                otm_call_returns[otm_pct] = (co_exit - co_entry) / co_entry if (co_entry and co_entry > 0) else np.nan

            # Bull call spread returns for several OTM widths
            spread_returns = {}
            for otm_pct in [0.10, 0.20, 0.30]:
                K2 = entry_price * (1 + otm_pct)
                s_entry = c_entry - bs_call_price(entry_price, K2, T_entry, risk_free_rate, sigma)
                s_exit  = c_exit  - bs_call_price(exit_price,  K2, T_exit,  risk_free_rate, sigma)
                s_exit = min(s_exit, K2 - K) if s_exit is not None else np.nan
                spread_returns[otm_pct] = (s_exit - s_entry) / s_entry if (s_entry and s_entry > 0) else np.nan
        else:
            option_return = np.nan
            otm_call_returns = {0.10: np.nan, 0.20: np.nan, 0.30: np.nan}
            spread_returns = {0.10: np.nan, 0.20: np.nan, 0.30: np.nan}

        rec = {
            "ticker": ticker,
            "recipient": row["Recipient Name"],
            "action_date": action_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "amount": amount,
            "agency": row.get("Awarding Agency", ""),
            "description": row.get("Transaction Description", ""),
            "fwd_return": fwd_return,
            "option_return": option_return,
            "call_return_10": otm_call_returns[0.10],
            "call_return_20": otm_call_returns[0.20],
            "call_return_30": otm_call_returns[0.30],
            "spread_return_10": spread_returns[0.10],
            "spread_return_20": spread_returns[0.20],
            "spread_return_30": spread_returns[0.30],
            "materiality": materiality,
            "market_cap": shares * entry_price if (shares and not np.isnan(shares)) else np.nan,
            "entry_price": entry_price,
            "sigma": sigma if (sigma and not np.isnan(sigma)) else np.nan,
        }
        records.append(rec)

    return pd.DataFrame(records)


def backtest_contract_strategy(events_df, min_materiality=None, top_k=10, hold_days=5,
                               max_market_cap=None, use_options=False, spread_otm_pct=None,
                               call_otm_pct=None):
    """
    Simulate a tranche-based portfolio.
    use_options=True uses ATM 1yr call return.
    call_otm_pct (0.10/0.20/0.30) uses single OTM call return instead.
    spread_otm_pct (0.10/0.20/0.30) uses bull call spread return instead.
    """
    if min_materiality is not None:
        events_df = events_df[events_df["materiality"] >= min_materiality]

    if max_market_cap is not None:
        events_df = events_df[events_df["market_cap"] <= max_market_cap]

    if call_otm_pct is not None:
        return_col = f"call_return_{int(call_otm_pct*100)}"
    elif spread_otm_pct is not None:
        return_col = f"spread_return_{int(spread_otm_pct*100)}"
    elif use_options:
        return_col = "option_return"
    else:
        return_col = "fwd_return"

    # Drop events with missing materiality or missing return
    events_df = events_df.dropna(subset=["materiality", return_col])

    # Build a map: entry_date -> mean return of selected batch
    batch_returns = {}
    for entry_date, group in events_df.groupby("entry_date"):
        # Deduplicate: one position per ticker per entry date (take largest materiality)
        group = group.sort_values("materiality", ascending=False).drop_duplicates("ticker")
        # Select top_k by materiality
        selected = group.nlargest(min(top_k, len(group)), "materiality")
        batch_returns[entry_date] = selected[return_col].mean()

    if not batch_returns:
        return pd.DataFrame(columns=["return"])

    # Get all trading days in range
    all_dates = sorted(batch_returns.keys())
    trading_days = pd.bdate_range(start=all_dates[0], end=all_dates[-1])

    # For each trading day, accumulate daily accrual from all active tranches
    # A tranche entered on day d is active on days d through d + hold_days - 1
    # Its daily accrual = fwd_return / hold_days (linear attribution)
    daily_accruals = {d: [] for d in trading_days}

    for entry_date, batch_ret in batch_returns.items():
        daily_accrual = batch_ret / hold_days
        # Find hold_days business days starting from entry_date
        hold_period = pd.bdate_range(start=entry_date, periods=hold_days)
        for d in hold_period:
            if d in daily_accruals:
                daily_accruals[d].append(daily_accrual)

    # Portfolio return = mean of active tranches weighted by 1/hold_days each
    # If n tranches active: portfolio_return = (1/hold_days) * sum(daily_accruals)
    # = mean(daily_accruals) when fully deployed (hold_days tranches active)
    portfolio_returns = []
    dates = []
    for d in trading_days:
        accruals = daily_accruals[d]
        if accruals:
            # Scale by fraction of capital deployed (n_active / hold_days)
            n_active = len(accruals)
            portfolio_ret = (n_active / hold_days) * np.mean(accruals)
            portfolio_returns.append(portfolio_ret)
            dates.append(d)

    results = pd.DataFrame({"return": portfolio_returns}, index=pd.DatetimeIndex(dates))
    return results


def evaluate_contract_backtest(results, label=''):
    r = results["return"]
    cum = (1 + r).cumprod()
    total = cum.iloc[-1] - 1
    n_years = (results.index[-1] - results.index[0]).days / 365.25
    ann_return = (1 + total) ** (1 / n_years) - 1 if n_years > 0 else 0
    sharpe = r.mean() / r.std() * np.sqrt(252 / 5) if r.std() > 0 else 0
    max_dd = ((cum / cum.cummax()) - 1).min()
    win_rate = (r > 0).mean()

    prefix = f"[{label}] " if label else ""
    print(f"{prefix}Total Return:     {total*100:.2f}%")
    print(f"{prefix}Ann. Return:      {ann_return*100:.2f}%")
    print(f"{prefix}Sharpe Ratio:     {sharpe:.2f}")
    print(f"{prefix}Max Drawdown:     {max_dd*100:.2f}%")
    print(f"{prefix}Win Rate:         {win_rate*100:.1f}%")
    print(f"{prefix}Years:            {n_years:.1f}")
    print(f"{prefix}Contract Events:  {len(results)}")

    return {"total_return": total, "ann_return": ann_return, "sharpe": sharpe, "max_drawdown": max_dd, "equity_curve": cum, "n_events": len(results)}


def plot_contract_backtest(dev_metrics, test_metrics, split_date=None):
    fig, ax = plt.subplots(figsize=(12, 5))
    dev_eq = dev_metrics["equity_curve"]
    # Rebase test curve to start exactly where dev ends
    test_eq = test_metrics["equity_curve"] / test_metrics["equity_curve"].iloc[0] * dev_eq.iloc[-1]
    ax.plot(dev_eq.index, dev_eq.values, color="blue", label="Dev Period")
    ax.plot(test_eq.index, test_eq.values, color="darkorange", label="Test Period")
    vline_date = pd.to_datetime(split_date) if split_date else dev_eq.index[-1]
    ax.axvline(x=vline_date, color="grey", linestyle="--", label="Dev / Test split")
    ax.set_title("USASpending Contract Signal — Equity Curve")
    ax.legend()
    ax.grid(True)
    plt.tight_layout()
    plt.show()
