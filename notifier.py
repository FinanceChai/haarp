"""
Telegram Notifier
==================
Optional module to send trade signals and scan summaries
to a Telegram bot. Useful for monitoring the bot remotely.

Setup:
  1. Message @BotFather on Telegram → /newbot → get token
  2. Message your bot, then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     to find your chat_id
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
"""

import logging
from typing import List, Optional

import requests

from polymarket_client import TradeSignal
from scanner import ScanResult

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends formatted notifications to a Telegram chat."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)

        if not self.enabled:
            logger.info("Telegram notifications disabled (no token/chat_id)")

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured chat."""
        if not self.enabled:
            return False

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    def notify_scan_result(self, result: ScanResult):
        """Send a formatted scan summary."""
        if not self.enabled:
            return

        if not result.opportunities and not result.errors:
            return  # Don't spam on empty scans

        lines = [
            f"🌡️ <b>Scan #{result.timestamp.strftime('%H:%M:%S')}</b>",
            f"Cities: {', '.join(result.cities_scanned)}",
            f"Markets: {result.markets_found} | Analyzed: {result.buckets_analyzed}",
            f"Duration: {result.scan_duration_ms:.0f}ms",
            "",
        ]

        if result.opportunities:
            lines.append(f"<b>📊 {len(result.opportunities)} Opportunities:</b>")
            for sig in result.opportunities:
                emoji = "🟢" if sig.confidence == "HIGH" else "🟡" if sig.confidence == "MEDIUM" else "⚪"
                lines.append(
                    f"{emoji} {sig.action} {sig.bucket.city} {sig.bucket.date} "
                    f"[{sig.bucket.bucket_low_f}-{sig.bucket.bucket_high_f}°F]\n"
                    f"   NOAA: {sig.noaa_probability:.0%} vs Mkt: {sig.market_price:.0%} "
                    f"| Edge: {sig.edge:.0%} | ${sig.size_usd:.2f}"
                )
            lines.append("")

        if result.blocked_signals:
            lines.append(f"🛑 {len(result.blocked_signals)} blocked by safeguards")

        if result.errors:
            lines.append(f"❌ {len(result.errors)} errors")
            for err in result.errors[:3]:  # Cap at 3 to avoid message overflow
                lines.append(f"  • {err[:100]}")

        self.send("\n".join(lines))

    def notify_trade_executed(self, signal: TradeSignal, response: dict):
        """Send notification when a trade is actually executed."""
        if not self.enabled:
            return

        emoji = "🟢" if signal.action == "BUY" else "🔴"
        text = (
            f"{emoji} <b>Trade Executed</b>\n"
            f"{signal.action} ${signal.size_usd:.2f} on "
            f"{signal.bucket.city} {signal.bucket.date} "
            f"[{signal.bucket.bucket_low_f}-{signal.bucket.bucket_high_f}°F]\n"
            f"NOAA: {signal.noaa_probability:.0%} | "
            f"Market: {signal.market_price:.0%} | "
            f"Edge: {signal.edge:.0%}\n"
            f"<i>{signal.reasoning[:200]}</i>"
        )
        self.send(text)

    def notify_error(self, error: str):
        """Send an error notification."""
        self.send(f"🚨 <b>Bot Error</b>\n{error[:500]}")

    def notify_startup(self, config_summary: str):
        """Send a startup notification."""
        self.send(
            f"🤖 <b>Weather Bot Started</b>\n\n"
            f"<pre>{config_summary}</pre>"
        )
