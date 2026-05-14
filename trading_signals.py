import argparse
import logging
import os
import platform
import subprocess
import traceback

import yaml
from dotenv import load_dotenv
from ib_insync import IB

from contract_strategy import (
    fetch_contracts_range, enrich_with_tickers, download_price_data,
    fetch_shares_outstanding, fetch_ticker_cik_map, build_8k_map,
    filter_preannounced, compute_forward_returns, backtest_contract_strategy,
    evaluate_contract_backtest, plot_contract_backtest,
)
from contract_live import run_contract_live

load_dotenv()


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def _ensure_ib_connected(ib, host="127.0.0.1", port=7497, client_id=3,
                         gateway_bat=None, max_retries=3, startup_wait=40):
    """
    Verify IB is connected; if not, optionally launch StartGateway.bat and retry.
    Returns True if connected, False if all retries exhausted.
    """
    import time
    for attempt in range(1, max_retries + 1):
        if ib.isConnected():
            return True

        logging.warning(f"IB not connected (attempt {attempt}/{max_retries})")

        if attempt == 1 and gateway_bat and os.path.exists(gateway_bat):
            logging.info(f"Launching IB Gateway: {gateway_bat}")
            subprocess.Popen([gateway_bat], shell=True)
            logging.info(f"Waiting {startup_wait}s for Gateway to initialise...")
            time.sleep(startup_wait)

        try:
            if ib.isConnected():
                ib.disconnect()
            ib.connect(host, port, clientId=client_id)
            logging.info("IB connection established.")
            return True
        except Exception as e:
            logging.error(f"Connection attempt {attempt} failed: {e}")
            time.sleep(10)

    return False


def main():
    parser = argparse.ArgumentParser(description="Government contract signal strategy")
    parser.add_argument(
        "routine",
        choices=["backtest_contract", "live_contract"],
        help="Routine to run",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate live_contract without placing any orders",
    )
    args = parser.parse_args()

    config = load_config("config.yaml")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("trading.log"),
            logging.StreamHandler(),
        ],
    )

    if args.routine == "backtest_contract":
        cfg = config["contract_strategy"]
        hold_days       = cfg["hold_days"]
        top_k           = cfg["top_k"]
        min_amount      = cfg["min_amount"]
        min_materiality = cfg.get("min_materiality")
        lookback_days   = cfg.get("8k_lookback_days", 28)
        max_market_cap  = cfg.get("max_market_cap")
        call_otm_pct    = cfg.get("call_otm_pct")

        logging.info("Fetching dev period contracts from USASpending...")
        dev_contracts = fetch_contracts_range(str(cfg["start_dev"]), str(cfg["end_dev"]), min_amount=min_amount)
        dev_contracts = enrich_with_tickers(dev_contracts)

        logging.info("Fetching test period contracts...")
        test_contracts = fetch_contracts_range(str(cfg["start_test"]), str(cfg["end_test"]), min_amount=min_amount)
        test_contracts = enrich_with_tickers(test_contracts)

        all_tickers = list(set(dev_contracts["ticker"].tolist() + test_contracts["ticker"].tolist()))
        logging.info(f"Downloading price data for {len(all_tickers)} tickers...")
        prices = download_price_data(all_tickers, start=str(cfg["start_dev"]), end=str(cfg["end_test"]))

        logging.info("Fetching shares outstanding...")
        shares_outstanding = fetch_shares_outstanding(all_tickers)

        logging.info("Fetching SEC CIK map and 8-K filing dates...")
        ticker_cik_map = fetch_ticker_cik_map()
        filing_dates_map = build_8k_map(all_tickers, ticker_cik_map)

        logging.info("Computing forward returns...")
        dev_events  = compute_forward_returns(dev_contracts,  prices, shares_outstanding, hold_days=hold_days)
        test_events = compute_forward_returns(test_contracts, prices, shares_outstanding, hold_days=hold_days)

        logging.info(f"Applying 8-K pre-announcement filter (lookback={lookback_days}d)...")
        dev_events  = filter_preannounced(dev_events,  filing_dates_map, lookback_days=lookback_days)
        test_events = filter_preannounced(test_events, filing_dates_map, lookback_days=lookback_days)

        # Resolve materiality — '0.25_mean' computes 0.25 × dev mean dynamically
        if isinstance(min_materiality, str) and "mean" in min_materiality:
            base_mean   = dev_events["materiality"].dropna().mean()
            multiplier  = float(min_materiality.replace("_mean", "")) if min_materiality != "mean" else 1.0
            min_materiality = base_mean * multiplier
            logging.info(f"Materiality threshold: {multiplier}x dev mean = {min_materiality:.4f}")

        if max_market_cap is not None:
            logging.info(f"Market cap filter: max ${max_market_cap/1e9:.0f}B")

        logging.info("Running backtests...")
        dev_results  = backtest_contract_strategy(dev_events,  min_materiality=min_materiality,
                                                  top_k=top_k, hold_days=hold_days,
                                                  max_market_cap=max_market_cap, call_otm_pct=call_otm_pct)
        test_results = backtest_contract_strategy(test_events, min_materiality=min_materiality,
                                                  top_k=top_k, hold_days=hold_days,
                                                  max_market_cap=max_market_cap, call_otm_pct=call_otm_pct)

        print("\n--- Dev Period ---")
        dev_metrics  = evaluate_contract_backtest(dev_results,  label="Dev")
        print("\n--- Test Period ---")
        test_metrics = evaluate_contract_backtest(test_results, label="Test")

        plot_contract_backtest(dev_metrics, test_metrics, split_date=cfg["end_dev"])

    elif args.routine == "live_contract":
        if platform.system() == "Windows":
            gateway_bat = config.get("ib_gateway_bat_win")
        else:
            gateway_bat = config.get("ib_gateway_bat_mac")

        ib = IB()
        if not _ensure_ib_connected(ib, gateway_bat=gateway_bat):
            logging.error("Could not connect to IB Gateway after retries — aborting.")
        else:
            try:
                run_contract_live(ib, config, dry_run=args.dry_run)
            except Exception as e:
                logging.error(f"live_contract crashed: {e}\n{traceback.format_exc()}")
            finally:
                ib.disconnect()
                logging.info("IB disconnected.")


if __name__ == "__main__":
    main()
