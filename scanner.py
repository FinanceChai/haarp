"""
Weather Market Scanner (Simmer-style)
======================================
The core engine that:
  1. Fetches NOAA/Open-Meteo point forecasts for configured cities
  2. Fetches Polymarket weather bucket prices
  3. Matches point forecast to the containing bucket
  4. Assigns flat probability (0.85) to the matching bucket
  5. Buys if matching bucket is priced below entry threshold
  6. Applies safeguards (slippage, flip-flop, time decay, Polymarket minimums)

Key difference from Gaussian approach: no per-bucket probability math.
Just: "NOAA says X°F → find bucket containing X → if price < 15¢, buy it."
"""

import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import TradingConfig, US_CITIES
from noaa_client import DailyForecast
from open_meteo_client import OpenMeteoClient
from polymarket_client import PolymarketClient, WeatherBucket, TradeSignal

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """Complete output of a single scan cycle."""
    timestamp: datetime
    cities_scanned: List[str]
    markets_found: int
    buckets_analyzed: int
    opportunities: List[TradeSignal]
    blocked_signals: List[Tuple[TradeSignal, str]]  # (signal, reason)
    errors: List[str]
    scan_duration_ms: float


class FlipFlopTracker:
    """
    Tracks recent trade direction changes per market to avoid
    whipsawing (buying then immediately selling the same bucket).
    """

    def __init__(self, window_hours: int = 6, max_flips: int = 2):
        self.window_hours = window_hours
        self.max_flips = max_flips
        self._history: Dict[str, List[Tuple[datetime, str]]] = {}  # market_id → [(time, action)]

    def record(self, market_id: str, action: str):
        self._history.setdefault(market_id, []).append(
            (datetime.now(timezone.utc), action)
        )

    def is_flip_flopping(self, market_id: str, proposed_action: str) -> bool:
        history = self._history.get(market_id, [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.window_hours)
        recent = [(t, a) for t, a in history if t > cutoff]

        if len(recent) < 2:
            return False

        # Count direction changes
        flips = 0
        for i in range(1, len(recent)):
            if recent[i][1] != recent[i - 1][1]:
                flips += 1

        if flips >= self.max_flips:
            logger.warning(f"Flip-flop detected for {market_id}: {flips} direction changes")
            return True

        return False


class WeatherScanner:
    """
    Scans NOAA forecasts against Polymarket weather buckets
    and generates trade signals.
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.noaa = NOAAClient()
        self.open_meteo = OpenMeteoClient()
        self.poly = PolymarketClient(config)
        self.flip_tracker = FlipFlopTracker(
            window_hours=config.flip_flop_window_hours,
            max_flips=config.max_flip_flops,
        )
        self._scan_count = 0

    def scan(self) -> ScanResult:
        """
        Run a complete scan cycle:
          1. Fetch NOAA forecasts for all configured cities
          2. Fetch Polymarket weather buckets
          3. Match forecasts to buckets
          4. Calculate edge and generate signals
          5. Apply safeguards
          6. Return filtered opportunities
        """
        import time as _time
        start = _time.time()
        self._scan_count += 1

        errors = []
        opportunities = []
        blocked = []

        # ── Step 1: Fetch forecasts (NOAA for US, Open-Meteo for international) ──
        forecasts: Dict[str, List[DailyForecast]] = {}
        for city in self.config.locations:
            try:
                if city in US_CITIES:
                    daily = self.noaa.get_daily_forecasts(city, days_ahead=3)
                else:
                    daily = self.open_meteo.get_daily_forecasts(city, days_ahead=3)
                if daily:
                    forecasts[city] = daily
            except Exception as e:
                source = "NOAA" if city in US_CITIES else "Open-Meteo"
                errors.append(f"{source} error for {city}: {e}")
                logger.error(f"{source} fetch failed for {city}: {e}")

        if not forecasts:
            return ScanResult(
                timestamp=datetime.now(timezone.utc),
                cities_scanned=self.config.locations,
                markets_found=0,
                buckets_analyzed=0,
                opportunities=[],
                blocked_signals=[],
                errors=errors or ["No forecast data available for any city"],
                scan_duration_ms=(_time.time() - start) * 1000,
            )

        # ── Step 2: Fetch Polymarket buckets ──
        try:
            buckets = self.poly.get_weather_buckets(city_filter=self.config.locations)
        except Exception as e:
            errors.append(f"Polymarket error: {e}")
            return ScanResult(
                timestamp=datetime.now(timezone.utc),
                cities_scanned=self.config.locations,
                markets_found=0,
                buckets_analyzed=0,
                opportunities=[],
                blocked_signals=[],
                errors=errors,
                scan_duration_ms=(_time.time() - start) * 1000,
            )

        # ── Step 3: Match point forecast to bucket (Simmer-style) ──
        # No Gaussian distribution — just find the bucket containing the forecast temp
        for city, flist in forecasts.items():
            logger.debug(f"  Forecast dates for {city}: {[f.date for f in flist]}")

        seen_bucket_dates = set()
        for bucket in buckets:
            city_forecasts = forecasts.get(bucket.city, [])
            if not city_forecasts:
                logger.debug(f"  No forecasts for city: {bucket.city}")
                continue

            # ── Hard constraint: Polymarket tick size ──
            market_price = bucket.yes_price
            if market_price < self.config.min_tick_size or market_price > (1.0 - self.config.min_tick_size):
                logger.debug(f"  Skip {bucket.city} {bucket.date} [{bucket.bucket_low_f}-{bucket.bucket_high_f}F]: price {market_price:.2f} at extreme")
                continue

            # Find matching date forecast
            matching_forecast = None
            for df in city_forecasts:
                if df.date == bucket.date:
                    matching_forecast = df
                    break

            if not matching_forecast:
                key = (bucket.city, bucket.date)
                if key not in seen_bucket_dates:
                    seen_bucket_dates.add(key)
                    avail = [f.date for f in city_forecasts]
                    logger.debug(
                        f"  No date match: {bucket.city} bucket={bucket.date!r} "
                        f"vs forecast={avail}"
                    )
                continue

            # Get the relevant temperature (high or low)
            forecast_temp = (
                matching_forecast.high_f if bucket.metric == "high"
                else matching_forecast.low_f
            )

            # Simmer-style: does the point forecast fall in this bucket?
            temp_in_bucket = bucket.bucket_low_f <= forecast_temp <= bucket.bucket_high_f

            # Flat probability: 0.85 if forecast is in bucket, 0.0 otherwise
            noaa_prob = self.config.noaa_flat_probability if temp_in_bucket else 0.0

            edge = noaa_prob - market_price

            logger.debug(
                f"  {bucket.city} {bucket.date} [{bucket.bucket_low_f}-{bucket.bucket_high_f}F] "
                f"| Forecast: {forecast_temp}F | In bucket: {temp_in_bucket} "
                f"| Prob: {noaa_prob:.0%} vs Market: {market_price:.0%} | Edge: {edge:.1%}"
            )

            # Only consider buckets where the forecast actually lands
            if not temp_in_bucket:
                continue

            # ── Hard constraint: minimum shares check ──
            if market_price > 0 and self.config.min_shares_per_order * market_price > self.config.max_position_usd:
                logger.debug(f"  Skip: min shares ({self.config.min_shares_per_order}) * price ({market_price:.2f}) > position size")
                continue

            # Determine action
            signal = self._evaluate_signal(bucket, noaa_prob, market_price, edge, forecast_temp)
            if signal is None:
                continue

            # ── Step 4: Apply safeguards ──
            block_reason = self._check_safeguards(signal, bucket)
            if block_reason:
                blocked.append((signal, block_reason))
                continue

            opportunities.append(signal)

        # Sort by edge (highest first) and cap at max_trades_per_scan
        opportunities.sort(key=lambda s: s.edge, reverse=True)
        opportunities = opportunities[: self.config.max_trades_per_scan]

        elapsed = (_time.time() - start) * 1000

        result = ScanResult(
            timestamp=datetime.now(timezone.utc),
            cities_scanned=list(forecasts.keys()),
            markets_found=len(buckets),
            buckets_analyzed=len(buckets),
            opportunities=opportunities,
            blocked_signals=blocked,
            errors=errors,
            scan_duration_ms=round(elapsed, 1),
        )

        self._log_scan_result(result)
        return result

    def _evaluate_signal(
        self,
        bucket: WeatherBucket,
        noaa_prob: float,
        market_price: float,
        edge: float,
        forecast_temp: float,
    ) -> Optional[TradeSignal]:
        """
        Simmer-style signal evaluation.

        BUY: forecast lands in this bucket AND price < entry_threshold (15¢).
             No Gaussian probability filter, no min_edge requirement.
             Logic: "NOAA says X, bucket contains X, price is cheap → buy."

        SELL: price >= exit_threshold (45¢) on a bucket we hold.
        """
        # ── BUY signal: matching bucket priced below entry threshold ──
        if market_price < self.config.entry_threshold and noaa_prob > 0:
            confidence = "HIGH" if edge > 0.60 else "MEDIUM" if edge > 0.40 else "LOW"

            # Smart sizing: min(balance * 5%, max_position), floor of $1.00
            size = max(
                self.config.min_position_usd,
                min(
                    self.config.max_position_usd,
                    self.config.max_total_exposure * self.config.balance_pct_per_trade,
                ),
            )

            return TradeSignal(
                bucket=bucket,
                noaa_probability=noaa_prob,
                market_price=market_price,
                edge=edge,
                expected_value=round(edge * (1 / market_price - 1) * size, 4) if market_price > 0 else 0,
                confidence=confidence,
                action="BUY",
                size_usd=size,
                reasoning=(
                    f"Forecast {forecast_temp}°F for {bucket.city} on {bucket.date} "
                    f"falls in bucket [{bucket.bucket_low_f}-{bucket.bucket_high_f}°F]. "
                    f"Bucket worth ~{noaa_prob:.0%} but priced at {market_price:.0%}. "
                    f"Edge: {edge:.0%}."
                ),
            )

        # ── SELL signal: price >= exit threshold ──
        if market_price >= self.config.exit_threshold:
            return TradeSignal(
                bucket=bucket,
                noaa_probability=noaa_prob,
                market_price=market_price,
                edge=market_price - noaa_prob,
                expected_value=0,
                confidence="MEDIUM",
                action="SELL",
                size_usd=0,  # Sell entire position
                reasoning=(
                    f"Market price {market_price:.0%} at/above exit threshold "
                    f"{self.config.exit_threshold:.0%}. Exit signal."
                ),
            )

        return None

    def _check_safeguards(self, signal: TradeSignal, bucket: WeatherBucket) -> Optional[str]:
        """
        Apply safety checks. Returns a block reason string, or None if clear.
        """
        # Check flip-flop
        if self.flip_tracker.is_flip_flopping(bucket.market_id, signal.action):
            return f"Flip-flop detected: too many direction changes on {bucket.market_id}"

        # Check time to resolution
        if bucket.end_date:
            try:
                end_dt = datetime.fromisoformat(bucket.end_date.replace("Z", "+00:00"))
                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < self.config.min_hours_to_resolution:
                    return f"Too close to resolution: {hours_left:.1f}h remaining"
            except (ValueError, TypeError):
                pass

        # Check slippage (only for BUY signals)
        if signal.action == "BUY" and bucket.token_id:
            slippage = self.poly.estimate_slippage(
                bucket.token_id, "BUY", signal.size_usd
            )
            if slippage > self.config.max_slippage_pct:
                return f"Estimated slippage {slippage:.1%} exceeds threshold {self.config.max_slippage_pct:.1%}"

        # Check minimum liquidity
        if bucket.liquidity < signal.size_usd * 2:
            return f"Insufficient liquidity: ${bucket.liquidity:.2f} for ${signal.size_usd:.2f} order"

        return None

    def _log_scan_result(self, result: ScanResult):
        """Pretty-print scan results to the logger."""
        logger.info(
            f"\n{'='*60}\n"
            f"🌡️  SCAN #{self._scan_count} — {result.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"{'='*60}\n"
            f"  Cities: {', '.join(result.cities_scanned)}\n"
            f"  Markets found: {result.markets_found}\n"
            f"  Buckets analyzed: {result.buckets_analyzed}\n"
            f"  Opportunities: {len(result.opportunities)}\n"
            f"  Blocked: {len(result.blocked_signals)}\n"
            f"  Errors: {len(result.errors)}\n"
            f"  Duration: {result.scan_duration_ms:.0f}ms\n"
        )

        for sig in result.opportunities:
            logger.info(
                f"  ✅ {sig.action} | {sig.bucket.city} {sig.bucket.date} "
                f"[{sig.bucket.bucket_low_f}-{sig.bucket.bucket_high_f}°F] | "
                f"NOAA: {sig.noaa_probability:.0%} vs Market: {sig.market_price:.0%} | "
                f"Edge: {sig.edge:.0%} | ${sig.size_usd:.2f} | {sig.confidence}"
            )

        for sig, reason in result.blocked_signals:
            logger.info(
                f"  🛑 BLOCKED {sig.action} | {sig.bucket.city} {sig.bucket.date} | {reason}"
            )

        for err in result.errors:
            logger.error(f"  ❌ {err}")
