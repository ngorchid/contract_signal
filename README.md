# Government Contract Signal Strategy

An event-driven trading strategy that uses US federal government contract awards as a price signal, executed via options to leverage the expected price move.

## Strategy Overview

When the US government awards a large contract to a public company, the award is disclosed on [USASpending.gov](https://usaspending.gov). This creates an information event that is:

- **Public** but not widely acted on — the data requires active retrieval and filtering
- **Material** for smaller companies where the award is large relative to market cap
- **Verifiable** against SEC 8-K filings to filter out pre-announced deals

The strategy buys a 30% out-of-the-money call option with ~1 year to expiry on the day the contract appears, holds for 60 trading days, then exits.

---

## Pipeline

```
USASpending.gov API
        │
        ▼
  Filter: new awards only (Mod=0), min $50M
        │
        ▼
  Map recipient names → public tickers
        │
        ▼
  Filter: materiality = award / market_cap ≥ threshold
        │
        ▼
  Filter: max market cap $100B (focus on mid-caps)
        │
        ▼
  Filter: no SEC 8-K filing in prior 28 days (removes pre-announced deals)
        │
        ▼
  Buy 30% OTM call option, ~1 year expiry
        │
        ▼
  Hold 60 trading days, then sell
```

### Key design choices

| Choice | Rationale |
|---|---|
| 30% OTM calls | ~3.5x leverage vs stock; reduces capital requirement by ~72% vs ATM |
| 60-day hold | Optimal from parameter scan: balances signal persistence vs time decay |
| Materiality filter | Award / market cap ≥ 0.25× mean — filters noise, retains ~1.4 signals/day |
| 8-K filter | Removes contracts already disclosed to market via SEC filing |
| Market cap ≤ $100B | Large caps (e.g. Microsoft) are unaffected by individual contract awards |
| Tranche-based portfolio | 1/60 of capital enters daily, creating a rolling diversified book |

---

## Backtest Results

| Period | Ann. Return | Sharpe | Max Drawdown |
|---|---|---|---|
| Dev (2015–2021) | 40.8% | 3.10 | -26.2% |
| Test (2022–2025) | 57.0% | 5.05 | -10.7% |

*Options returns modelled using Black-Scholes with 60-day historical volatility. Time decay is accounted for at exit.*

---

## File Structure

```
├── contract_strategy.py   # Backtesting: data fetching, filtering, return computation
├── contract_live.py       # Live execution: IB orders, position tracking, email report
├── trading_signals.py     # Entry point (CLI)
├── config.yaml            # All parameters
├── .env.example           # Required environment variables (copy to .env)
└── requirements.txt
```

Runtime files created automatically (excluded from git):
- `contract_cache/` — cached USASpending API responses
- `edgar_cache/` — cached SEC 8-K filing dates
- `contract_positions.json` — open option positions
- `contract_trade_log.json` — closed trade history + cumulative P&L
- `contract_processed_ids.json` — processed Award IDs (prevents double-buying)

---

## Setup

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Configure environment**
```bash
cp .env.example .env
# Edit .env with your email credentials and SEC contact email
```

**3. Configure parameters**

Edit `config.yaml`. Key fields:
- `min_materiality_live` — absolute materiality threshold for live trading (resolve once by running the backtest and reading the logged value)
- `ib_gateway_bat_win` / `ib_gateway_bat_mac` — path to your IB Gateway startup script

**4. IB Gateway**

Live trading requires [Interactive Brokers](https://www.interactivebrokers.com) with:
- TWS or IB Gateway running with API enabled (port 7497)
- Options market data subscription (OPRA)

---

## Usage

**Run backtest**
```bash
python trading_signals.py backtest_contract
```

**Run live (paper trading first)**
```bash
# Dry run — full pipeline, no orders placed
python trading_signals.py live_contract --dry-run

# Live
python trading_signals.py live_contract
```

**Scheduling (run 30 min before market close)**

On Windows via Task Scheduler:
```
Program: python
Arguments: trading_signals.py live_contract
Start in: C:\path\to\this\repo
Time: 15:30 daily, Mon–Fri
```

---

## Data Sources

| Source | Used for |
|---|---|
| [USASpending.gov](https://usaspending.gov) | Federal contract awards (free, no API key needed) |
| [SEC EDGAR](https://www.sec.gov/cgi-bin/browse-edgar) | 8-K filing dates for pre-announcement filter (free) |
| [yfinance](https://github.com/ranaroussi/yfinance) | Historical prices and shares outstanding |
| [Interactive Brokers API](https://www.interactivebrokers.com/en/trading/ib-api.php) | Live order execution and option pricing |

---

## Limitations

- **Static ticker universe** — only ~60 publicly traded contractors are mapped; awards to private companies or unknown subsidiaries are missed
- **Reporting lag** — agencies can take up to 5 days to report awards; the strategy handles this with a configurable lookback window
- **Historical vol** — option returns are modelled using realised vol, not implied vol; live execution costs may differ
- **Survivorship bias** — the ticker map was built from known contractors; companies that went private or were acquired during the backtest period may be overstated
- **Backtest signal frequency** — ~1.4 buys per active day; many days have no signal

---

## Disclaimer

This project is for research and educational purposes. Past backtest performance does not guarantee future results. Options trading involves substantial risk of loss.
