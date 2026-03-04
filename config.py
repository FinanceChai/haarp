"""
Polymarket Weather Trading Bot — Configuration
================================================
All configurable parameters in one place.
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple

# ─────────────────────────────────────────────
# CITY COORDINATES (lat, lon) for weather lookups
# ─────────────────────────────────────────────
CITY_COORDS: Dict[str, Tuple[float, float]] = {
    # US cities (NOAA)
    "NYC":           (40.7128, -74.0060),
    "Chicago":       (41.8781, -87.6298),
    "Seattle":       (47.6062, -122.3321),
    "Atlanta":       (33.7490, -84.3880),
    "Dallas":        (32.7767, -96.7970),
    "Miami":         (25.7617, -80.1918),
    # International cities (Open-Meteo)
    "Ankara":        (39.9334,  32.8597),
    "Buenos Aires":  (-34.6037, -58.3816),
    "London":        (51.5074,  -0.1278),
    "Lucknow":       (26.8467,  80.9462),
    "Munich":        (48.1351,  11.5820),
    "Paris":         (48.8566,   2.3522),
    "Sao Paulo":     (-23.5505, -46.6333),
    "Seoul":         (37.5665, 126.9780),
    "Toronto":       (43.6532, -79.3832),
    "Wellington":    (-41.2924, 174.7787),
}

# Cities served by NOAA (US-only). Everything else uses Open-Meteo.
US_CITIES = {"NYC", "Chicago", "Seattle", "Atlanta", "Dallas", "Miami"}


@dataclass
class TradingConfig:
    """Bot trading parameters — tune these to your risk appetite."""

    # ── Entry / Exit (Gaussian + mispricing ratio) ──
    entry_threshold: float = 0.25      # Only buy buckets priced below 25¢
    exit_threshold: float = 0.45       # Sell when market corrects above 45¢
    min_mispricing_ratio: float = 2.0  # Buy when noaa_prob / market_price >= this ratio

    # ── Polymarket minimums ──
    min_shares_per_order: float = 5.0  # Polymarket requires at least 5 shares
    min_tick_size: float = 0.01        # Skip buckets priced < $0.01 or > $0.99

    # ── Position sizing ──
    max_position_usd: float = 2.00     # Max $ per single position
    max_total_exposure: float = 100.00  # Max aggregate exposure across all positions
    balance_pct_per_trade: float = 0.05 # Smart sizing: use 5% of available balance per trade
    min_position_usd: float = 1.00     # Floor for position sizing

    # ── Risk controls ──
    max_trades_per_scan: int = 5       # Hard cap per scan cycle
    min_hours_to_resolution: int = 2   # Skip markets resolving within this window
    max_slippage_pct: float = 0.15     # Abort if estimated slippage exceeds 15%
    flip_flop_window_hours: int = 6    # Detect direction reversals within this window
    max_flip_flops: int = 2            # Max allowed direction changes before blocking
    price_drop_threshold: float = 0.10 # Flag 10%+ drops in 24h (informational, doesn't block)

    # ── Scan settings ──
    scan_interval_seconds: int = 120   # How often to scan (2 minutes)
    locations: list = field(default_factory=lambda: list(CITY_COORDS.keys()))

    # ── Polymarket API ──
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137  # Polygon

    # ── Logging ──
    log_file: str = "weather_bot.log"
    verbose: bool = True


@dataclass
class Secrets:
    """
    Sensitive credentials — load from environment variables.
    NEVER commit these to source control.
    """
    polymarket_private_key: str = ""
    polymarket_funder_address: str = ""
    telegram_bot_token: str = ""       # Optional: for notifications
    telegram_chat_id: str = ""         # Optional: for notifications
