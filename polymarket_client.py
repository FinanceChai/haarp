"""
Polymarket Client
==================
Interfaces with Polymarket's Gamma API (market discovery) and
CLOB API (order book + trading).

Weather markets on Polymarket follow patterns like:
  - "NYC high temperature on March 5" with buckets like "70-72°F", "72-74°F", etc.
  - Each bucket is a binary YES/NO market with a token_id

This module:
  1. Discovers active weather/temperature markets via Gamma API
  2. Parses bucket ranges from market titles
  3. Reads order books for pricing
  4. (Optionally) places trades via CLOB API

For read-only scanning, no API key is needed.
For trading, you need a funded Polygon wallet + py-clob-client.
"""

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

from config import TradingConfig

logger = logging.getLogger(__name__)


@dataclass
class WeatherBucket:
    """A single temperature bucket on Polymarket."""
    market_id: str           # Gamma market/condition ID
    token_id: str            # CLOB token ID for YES outcome
    token_id_no: str         # CLOB token ID for NO outcome
    city: str                # Parsed city name
    date: str                # Target date (YYYY-MM-DD)
    metric: str              # "high" or "low" temperature
    bucket_low_f: float      # Lower bound of bucket (°F)
    bucket_high_f: float     # Upper bound of bucket (°F)
    question: str            # Full market question text
    yes_price: float         # Current YES price (0-1)
    no_price: float          # Current NO price (0-1)
    volume: float            # Total traded volume
    liquidity: float         # Available liquidity
    end_date: Optional[str] = None  # Market resolution date
    spread: float = 0.0      # Bid-ask spread


@dataclass
class TradeSignal:
    """A trading opportunity identified by the scanner."""
    bucket: WeatherBucket
    noaa_probability: float  # Our estimated probability (0-1)
    market_price: float      # Current market price (YES)
    edge: float              # noaa_probability - market_price
    expected_value: float    # edge * potential_payout
    confidence: str          # "HIGH", "MEDIUM", "LOW"
    action: str              # "BUY" or "SELL"
    size_usd: float          # Recommended position size
    reasoning: str           # Human-readable explanation


class PolymarketClient:
    """
    Read-only client for discovering and pricing weather markets.
    For actual trade execution, use PolymarketTrader (requires keys).
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolyWeatherBot/1.0",
            "Accept": "application/json",
        })

    # ──────────────────────────────────────
    # Market Discovery via Gamma API
    # ──────────────────────────────────────

    def fetch_weather_markets(self) -> List[Dict]:
        """
        Search Gamma API for active weather/temperature markets.
        Returns raw market data dicts.
        """
        markets = []
        cursor = ""
        keywords = ["temperature", "high temp", "weather"]

        # Fetch weather events using tag_id (weather = 84)
        try:
            url = f"{self.config.gamma_host}/events"
            params = {
                "active": "true",
                "closed": "false",
                "tag_id": 84,
                "limit": 100,
            }
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            events = resp.json()

            if isinstance(events, list):
                for event in events:
                    for mkt in event.get("markets", []):
                        markets.append(mkt)

        except requests.RequestException as e:
            logger.warning(f"Gamma events query failed: {e}")

        # Also fetch from markets endpoint with tag_id
        for keyword in keywords:
            try:
                url = f"{self.config.gamma_host}/markets"
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "tag_id": 84,
                }
                resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, list):
                    markets.extend(data)
                elif isinstance(data, dict) and "data" in data:
                    markets.extend(data["data"])
                break  # Only need one fetch since tag_id is the filter now

            except requests.RequestException as e:
                logger.warning(f"Gamma API query failed for '{keyword}': {e}")
                continue

        # Deduplicate by condition_id
        seen = set()
        unique = []
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id") or m.get("id", "")
            if cid not in seen:
                seen.add(cid)
                unique.append(m)

        logger.info(f"Found {len(unique)} unique weather markets")
        return unique

    # ──────────────────────────────────────
    # Bucket Parsing
    # ──────────────────────────────────────

    @staticmethod
    def parse_temperature_bucket(question: str) -> Optional[Dict]:
        """
        Parse a Polymarket weather market question into structured data.

        Examples it handles:
          "Will the high temperature in NYC on March 5 be 72-74°F?"
          "NYC high temperature March 5, 2026: 70°F to 72°F"
          "What will the high temp in Chicago be on 3/5? 68-70°F"
        """
        result = {}

        # Extract city
        city_patterns = {
            "NYC": r"\bNYC\b|New York",
            "Chicago": r"\bChicago\b",
            "Seattle": r"\bSeattle\b",
            "Atlanta": r"\bAtlanta\b",
            "Dallas": r"\bDallas\b",
            "Miami": r"\bMiami\b",
        }
        for city, pattern in city_patterns.items():
            if re.search(pattern, question, re.IGNORECASE):
                result["city"] = city
                break

        if "city" not in result:
            return None

        # Extract temperature bucket range
        # Patterns: "72-74°F", "72°F to 74°F", "72-74 °F", "72 to 74°F"
        temp_patterns = [
            r"(\d+)\s*[-–]\s*(\d+)\s*°?\s*F",
            r"(\d+)\s*°?\s*F?\s*to\s*(\d+)\s*°?\s*F",
            r"above\s+(\d+)\s*°?\s*F",
            r"below\s+(\d+)\s*°?\s*F",
            r"over\s+(\d+)\s*°?\s*F",
            r"under\s+(\d+)\s*°?\s*F",
        ]

        for pattern in temp_patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    result["bucket_low_f"] = float(groups[0])
                    result["bucket_high_f"] = float(groups[1])
                elif "above" in pattern or "over" in pattern:
                    result["bucket_low_f"] = float(groups[0])
                    result["bucket_high_f"] = float(groups[0]) + 20  # open-ended
                elif "below" in pattern or "under" in pattern:
                    result["bucket_low_f"] = float(groups[0]) - 20
                    result["bucket_high_f"] = float(groups[0])
                break

        if "bucket_low_f" not in result:
            return None

        # Extract date
        # Try various date formats — use explicit month names to avoid
        # matching temperature numbers like "between 34" as dates
        months = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        date_patterns = [
            (rf"({months}\s+\d{{1,2}},?\s*\d{{4}})", "%B %d, %Y"),
            (rf"({months}\s+\d{{1,2}},?\s*\d{{4}})", "%B %d %Y"),
            (r"(\d{1,2}/\d{1,2}/?\d{0,4})", None),  # Handle separately
            (rf"({months}\s+\d{{1,2}})\b", None),  # "March 5" — assume current year
        ]

        for pattern, fmt in date_patterns:
            match = re.search(pattern, question)
            if match:
                date_str = match.group(1).strip().rstrip(",")
                try:
                    if fmt:
                        dt = datetime.strptime(date_str, fmt)
                        result["date"] = dt.strftime("%Y-%m-%d")
                    elif "/" in date_str:
                        parts = date_str.split("/")
                        month, day = int(parts[0]), int(parts[1])
                        year = int(parts[2]) if len(parts) > 2 and parts[2] else datetime.now().year
                        result["date"] = f"{year:04d}-{month:02d}-{day:02d}"
                    else:
                        # "March 5" — assume current year
                        dt = datetime.strptime(f"{date_str} {datetime.now().year}", "%B %d %Y")
                        result["date"] = dt.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

        # High vs low
        if re.search(r"\bhigh\b", question, re.IGNORECASE):
            result["metric"] = "high"
        elif re.search(r"\blow\b", question, re.IGNORECASE):
            result["metric"] = "low"
        else:
            result["metric"] = "high"  # Default assumption

        return result

    # ──────────────────────────────────────
    # Build Structured Buckets
    # ──────────────────────────────────────

    def get_weather_buckets(self, city_filter: Optional[List[str]] = None) -> List[WeatherBucket]:
        """
        Fetch all active weather markets and parse into WeatherBucket objects.
        Optionally filter by city names.
        """
        raw_markets = self.fetch_weather_markets()
        buckets = []

        for m in raw_markets:
            question = m.get("question", "") or m.get("title", "") or ""
            parsed = self.parse_temperature_bucket(question)

            if parsed is None:
                continue

            if city_filter and parsed.get("city") not in city_filter:
                continue

            # Extract token IDs and prices
            tokens = m.get("clobTokenIds") or m.get("clob_token_ids")
            outcomes = m.get("outcomes") or m.get("outcomePrices")
            prices = m.get("outcomePrices") or []

            if isinstance(tokens, str):
                tokens = eval(tokens) if tokens.startswith("[") else [tokens]
            if isinstance(prices, str):
                prices = eval(prices) if prices.startswith("[") else [prices]

            token_yes = tokens[0] if tokens and len(tokens) > 0 else ""
            token_no = tokens[1] if tokens and len(tokens) > 1 else ""

            yes_price = 0.0
            no_price = 0.0
            if prices and len(prices) >= 2:
                try:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
                except (ValueError, TypeError):
                    pass

            bucket = WeatherBucket(
                market_id=m.get("conditionId") or m.get("condition_id") or m.get("id", ""),
                token_id=token_yes,
                token_id_no=token_no,
                city=parsed["city"],
                date=parsed.get("date", ""),
                metric=parsed.get("metric", "high"),
                bucket_low_f=parsed["bucket_low_f"],
                bucket_high_f=parsed["bucket_high_f"],
                question=question,
                yes_price=yes_price,
                no_price=no_price,
                volume=float(m.get("volume", 0) or 0),
                liquidity=float(m.get("liquidity", 0) or 0),
                end_date=m.get("endDate") or m.get("end_date_iso"),
            )
            buckets.append(bucket)

        logger.info(f"Parsed {len(buckets)} weather buckets (filtered: {city_filter})")
        return buckets

    # ──────────────────────────────────────
    # Order Book / Pricing (CLOB API)
    # ──────────────────────────────────────

    def get_order_book(self, token_id: str) -> Optional[Dict]:
        """Fetch the order book for a token from the CLOB API."""
        if not token_id:
            return None

        try:
            url = f"{self.config.clob_host}/book"
            params = {"token_id": token_id}
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"Order book fetch failed for {token_id}: {e}")
            return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get the midpoint price for a token."""
        if not token_id:
            return None

        try:
            url = f"{self.config.clob_host}/midpoint"
            params = {"token_id": token_id}
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0))
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Midpoint fetch failed for {token_id}: {e}")
            return None

    def estimate_slippage(self, token_id: str, side: str, size_usd: float) -> float:
        """
        Estimate slippage for a given order size by walking the order book.
        Returns estimated fill price vs midpoint as a fraction.
        """
        book = self.get_order_book(token_id)
        if not book:
            return 1.0  # Max slippage — can't estimate

        mid = self.get_midpoint(token_id)
        if not mid or mid == 0:
            return 1.0

        # Walk the relevant side of the book
        orders = book.get("asks" if side == "BUY" else "bids", [])
        if not orders:
            return 1.0

        remaining = size_usd
        weighted_price = 0.0
        total_filled = 0.0

        for order in orders:
            price = float(order.get("price", 0))
            size = float(order.get("size", 0))
            fill_value = min(remaining, size * price)
            fill_shares = fill_value / price if price > 0 else 0

            weighted_price += price * fill_shares
            total_filled += fill_shares
            remaining -= fill_value

            if remaining <= 0:
                break

        if total_filled == 0:
            return 1.0

        avg_fill = weighted_price / total_filled
        slippage = abs(avg_fill - mid) / mid
        return round(slippage, 4)


class PolymarketTrader:
    """
    Executes trades on Polymarket via py-clob-client.
    Requires: pip install py-clob-client

    Initialize with private key + funder address.
    """

    def __init__(self, config: TradingConfig, private_key: str, funder: str):
        self.config = config
        self.private_key = private_key
        self.funder = funder
        self._client = None

    def _ensure_client(self):
        """Lazy-init the CLOB client (requires py-clob-client installed)."""
        if self._client is not None:
            return

        try:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(
                self.config.clob_host,
                key=self.private_key,
                chain_id=self.config.chain_id,
                signature_type=1,
                funder=self.funder,
            )
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            logger.info("CLOB client initialized successfully")
        except ImportError:
            raise ImportError(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )

    def place_market_order(self, token_id: str, amount_usd: float, side: str = "BUY") -> Dict:
        """
        Place a fill-or-kill market order.

        Args:
            token_id: The CLOB token ID for the YES/NO outcome
            amount_usd: Dollar amount to trade
            side: "BUY" or "SELL"

        Returns:
            Order response dict
        """
        self._ensure_client()

        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = BUY if side.upper() == "BUY" else SELL

        order = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=order_side,
            order_type=OrderType.FOK,
        )

        signed = self._client.create_market_order(order)
        resp = self._client.post_order(signed, OrderType.FOK)

        logger.info(f"Market order placed: {side} ${amount_usd} on {token_id[:12]}... → {resp}")
        return resp

    def place_limit_order(
        self, token_id: str, price: float, size: float, side: str = "BUY"
    ) -> Dict:
        """
        Place a GTC limit order.

        Args:
            token_id: The CLOB token ID
            price: Limit price (0-1)
            size: Number of shares
            side: "BUY" or "SELL"

        Returns:
            Order response dict
        """
        self._ensure_client()

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = BUY if side.upper() == "BUY" else SELL

        order = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=order_side,
        )

        signed = self._client.create_order(order)
        resp = self._client.post_order(signed, OrderType.GTC)

        logger.info(f"Limit order: {side} {size} shares @ {price} on {token_id[:12]}... → {resp}")
        return resp

    def get_positions(self) -> List[Dict]:
        """Fetch current open positions."""
        self._ensure_client()
        try:
            from py_clob_client.clob_types import OpenOrderParams
            return self._client.get_orders(OpenOrderParams()) or []
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def cancel_all(self):
        """Cancel all open orders."""
        self._ensure_client()
        self._client.cancel_all()
        logger.info("All open orders cancelled")
