"""
Polymarket Weather Trading Bot — Main Runner
==============================================

Usage:
  # Scan only (read-only, no trades)
  python main.py scan

  # Scan once with detailed output
  python main.py scan --once --verbose

  # Run the full trading loop (requires wallet keys)
  python main.py trade

  # Dry run — scan + generate signals, log what WOULD trade
  python main.py trade --dry-run

  # Backtest mode — fetch historical data and simulate
  python main.py backtest --days 7

Environment variables (or .env file):
  POLYMARKET_PRIVATE_KEY     - Your Polygon wallet private key
  POLYMARKET_FUNDER_ADDRESS  - Your funder/proxy wallet address
  TELEGRAM_BOT_TOKEN         - Optional: Telegram bot token
  TELEGRAM_CHAT_ID           - Optional: Telegram chat ID
"""

import os
import sys
import time
import json
import signal
import logging
import argparse
from datetime import datetime, timezone
from typing import Optional

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import TradingConfig, Secrets
from scanner import WeatherScanner, ScanResult
from polymarket_client import PolymarketTrader, TradeSignal
from notifier import TelegramNotifier

# ─────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────

def setup_logging(verbose: bool = False, log_file: str = "weather_bot.log"):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # Wrap stdout in UTF-8 for Windows (handles emoji in both logging and print)
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)

    console = logging.StreamHandler(sys.stdout)
    handlers = [
        console,
        logging.FileHandler(log_file, mode="a", encoding="utf-8"),
    ]

    logging.basicConfig(level=level, format=fmt, handlers=handlers)

    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ─────────────────────────────────────────
# Bot Controller
# ─────────────────────────────────────────

class WeatherBot:
    """
    Main controller that ties together scanning, trading, and notifications.
    """

    def __init__(
        self,
        config: TradingConfig,
        secrets: Secrets,
        dry_run: bool = True,
    ):
        self.config = config
        self.secrets = secrets
        self.dry_run = dry_run
        self.running = False
        self.logger = logging.getLogger("WeatherBot")

        # Initialize components
        self.scanner = WeatherScanner(config)
        self.notifier = TelegramNotifier(secrets.telegram_bot_token, secrets.telegram_chat_id)

        # Trader (only if not dry run and keys provided)
        self.trader: Optional[PolymarketTrader] = None
        if not dry_run and secrets.polymarket_private_key:
            self.trader = PolymarketTrader(
                config, secrets.polymarket_private_key, secrets.polymarket_funder_address
            )

        # Stats
        self.total_scans = 0
        self.total_trades = 0
        self.total_signals = 0
        self.total_blocked = 0

    def start(self):
        """Start the continuous scan loop."""
        self.running = True

        mode = "DRY RUN" if self.dry_run else "LIVE TRADING"
        config_summary = self._config_summary()
        self.logger.info(f"\n{'='*60}\n🤖 Weather Bot Starting ({mode})\n{'='*60}\n{config_summary}")
        self.notifier.notify_startup(config_summary)

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self.running:
            try:
                self._run_cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.error(f"Cycle error: {e}", exc_info=True)
                self.notifier.notify_error(str(e))

            if self.running:
                self.logger.debug(
                    f"Sleeping {self.config.scan_interval_seconds}s until next scan..."
                )
                time.sleep(self.config.scan_interval_seconds)

        self._print_summary()

    def scan_once(self) -> ScanResult:
        """Run a single scan and return results (no trading)."""
        return self.scanner.scan()

    def _run_cycle(self):
        """Execute one scan → analyze → (optionally) trade cycle."""
        self.total_scans += 1

        # Scan
        result = self.scanner.scan()
        self.total_signals += len(result.opportunities)
        self.total_blocked += len(result.blocked_signals)

        # Notify
        self.notifier.notify_scan_result(result)

        # Execute trades
        if result.opportunities and not self.dry_run and self.trader:
            for signal in result.opportunities:
                try:
                    self._execute_trade(signal)
                except Exception as e:
                    self.logger.error(f"Trade execution failed: {e}", exc_info=True)
                    self.notifier.notify_error(f"Trade failed: {e}")
        elif result.opportunities and self.dry_run:
            for sig in result.opportunities:
                self.logger.info(
                    f"  [DRY RUN] Would {sig.action} ${sig.size_usd:.2f} on "
                    f"{sig.bucket.city} {sig.bucket.date} "
                    f"[{sig.bucket.bucket_low_f}-{sig.bucket.bucket_high_f}°F] "
                    f"(edge: {sig.edge:.0%})"
                )

    def _execute_trade(self, signal: TradeSignal):
        """Execute a single trade signal."""
        if not self.trader:
            return

        if signal.action == "BUY":
            resp = self.trader.place_market_order(
                token_id=signal.bucket.token_id,
                amount_usd=signal.size_usd,
                side="BUY",
            )
        elif signal.action == "SELL":
            resp = self.trader.place_market_order(
                token_id=signal.bucket.token_id,
                amount_usd=signal.size_usd,
                side="SELL",
            )
        else:
            return

        self.total_trades += 1
        self.scanner.flip_tracker.record(signal.bucket.market_id, signal.action)
        self.notifier.notify_trade_executed(signal, resp)

        self.logger.info(
            f"  ✅ EXECUTED: {signal.action} ${signal.size_usd:.2f} on "
            f"{signal.bucket.city} [{signal.bucket.bucket_low_f}-{signal.bucket.bucket_high_f}°F]"
        )

    def _shutdown(self, signum, frame):
        """Graceful shutdown handler."""
        self.logger.info("\n⏹️  Shutdown signal received. Finishing current cycle...")
        self.running = False

    def _config_summary(self) -> str:
        c = self.config
        return (
            f"Entry threshold:  {c.entry_threshold:.0%}\n"
            f"Exit threshold:   {c.exit_threshold:.0%}\n"
            f"Min edge:         {c.min_edge:.0%}\n"
            f"Max position:     ${c.max_position_usd:.2f}\n"
            f"Max exposure:     ${c.max_total_exposure:.2f}\n"
            f"Locations:        {', '.join(c.locations)}\n"
            f"Scan interval:    {c.scan_interval_seconds}s\n"
            f"Max trades/scan:  {c.max_trades_per_scan}\n"
            f"Safeguards:       ON\n"
            f"Mode:             {'DRY RUN' if self.dry_run else 'LIVE'}"
        )

    def _print_summary(self):
        self.logger.info(
            f"\n{'='*60}\n"
            f"📊 Session Summary\n"
            f"{'='*60}\n"
            f"  Total scans:    {self.total_scans}\n"
            f"  Total signals:  {self.total_signals}\n"
            f"  Total blocked:  {self.total_blocked}\n"
            f"  Total trades:   {self.total_trades}\n"
            f"{'='*60}"
        )


# ─────────────────────────────────────────
# CLI
# ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Weather Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py scan                  # Scan markets (read-only)
  python main.py scan --once           # Single scan, then exit
  python main.py trade --dry-run       # Full loop, no real trades
  python main.py trade                 # Live trading (requires keys)
        """,
    )

    parser.add_argument(
        "mode",
        choices=["scan", "trade"],
        help="'scan' = read-only monitoring, 'trade' = execute trades",
    )
    parser.add_argument("--once", action="store_true", help="Run a single scan then exit")
    parser.add_argument("--dry-run", action="store_true", help="Log trades without executing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug-level logging")
    parser.add_argument("--interval", type=int, default=120, help="Scan interval in seconds")
    parser.add_argument(
        "--cities",
        type=str,
        default=None,
        help="Comma-separated city list (e.g., NYC,Chicago,Miami)",
    )
    parser.add_argument("--max-position", type=float, default=2.0, help="Max $ per position")
    parser.add_argument("--entry-threshold", type=float, default=0.15, help="Entry threshold (0-1)")
    parser.add_argument("--exit-threshold", type=float, default=0.45, help="Exit threshold (0-1)")
    parser.add_argument("--min-edge", type=float, default=0.20, help="Minimum edge to trade (0-1)")

    args = parser.parse_args()

    # Setup
    setup_logging(verbose=args.verbose)

    # Build config
    config = TradingConfig(
        scan_interval_seconds=args.interval,
        max_position_usd=args.max_position,
        entry_threshold=args.entry_threshold,
        exit_threshold=args.exit_threshold,
        min_edge=args.min_edge,
    )
    if args.cities:
        config.locations = [c.strip() for c in args.cities.split(",")]

    # Load secrets from env
    secrets = Secrets(
        polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
        polymarket_funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    # Determine mode
    dry_run = args.mode == "scan" or args.dry_run

    if args.mode == "trade" and not dry_run and not secrets.polymarket_private_key:
        print("❌ POLYMARKET_PRIVATE_KEY not set. Use --dry-run or set the env variable.")
        sys.exit(1)

    # Create bot
    bot = WeatherBot(config=config, secrets=secrets, dry_run=dry_run)

    if args.once:
        # Single scan
        result = bot.scan_once()
        print(f"\n📊 Found {len(result.opportunities)} opportunities, "
              f"{len(result.blocked_signals)} blocked, "
              f"{len(result.errors)} errors")

        for sig in result.opportunities:
            print(
                f"\n  {'🟢' if sig.confidence == 'HIGH' else '🟡'} {sig.action} "
                f"{sig.bucket.city} {sig.bucket.date} "
                f"[{sig.bucket.bucket_low_f}-{sig.bucket.bucket_high_f}°F]"
            )
            print(f"    NOAA: {sig.noaa_probability:.1%} vs Market: {sig.market_price:.1%}")
            print(f"    Edge: {sig.edge:.1%} | Size: ${sig.size_usd:.2f} | {sig.confidence}")
            print(f"    {sig.reasoning}")
    else:
        # Continuous loop
        bot.start()


if __name__ == "__main__":
    main()
