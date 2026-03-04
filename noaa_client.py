"""
NOAA Forecast Fetcher
======================
Pulls hourly and daily forecasts from the NWS API (api.weather.gov).
Free, no API key required. Rate-limited but generous for typical use.

Flow:
  1. /points/{lat},{lon}  →  get grid office + gridX/gridY
  2. /gridpoints/{office}/{gridX},{gridY}/forecast         →  12h period forecasts
  3. /gridpoints/{office}/{gridX},{gridY}/forecast/hourly   →  hourly forecasts

We cache the grid lookup since it rarely changes.
"""

import time
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from config import CITY_COORDS

logger = logging.getLogger(__name__)

# NOAA requires a User-Agent header identifying your application
HEADERS = {
    "User-Agent": "(PolyWeatherBot, contact@example.com)",
    "Accept": "application/geo+json",
}

BASE_URL = "https://api.weather.gov"


@dataclass
class HourlyForecast:
    """Single hourly forecast period from NOAA."""
    city: str
    start_time: datetime
    end_time: datetime
    temperature_f: float
    temperature_c: float
    is_daytime: bool
    short_forecast: str  # e.g., "Partly Cloudy"
    wind_speed: str
    wind_direction: str
    probability_of_precipitation: Optional[float] = None  # 0-100


@dataclass
class DailyForecast:
    """Aggregated daily forecast with high/low."""
    city: str
    date: str  # YYYY-MM-DD
    high_f: float
    low_f: float
    high_c: float
    low_c: float
    daytime_forecast: str
    night_forecast: str
    hourly_temps_f: List[float] = field(default_factory=list)


@dataclass
class GridInfo:
    """Cached NOAA grid lookup for a coordinate."""
    office: str
    grid_x: int
    grid_y: int
    fetched_at: float = 0.0


class NOAAClient:
    """
    Fetches and parses NOAA weather forecasts.

    Usage:
        client = NOAAClient()
        forecasts = client.get_daily_forecasts("NYC")
        hourly = client.get_hourly_forecasts("NYC")
    """

    def __init__(self, cache_ttl_hours: float = 24.0):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._grid_cache: Dict[str, GridInfo] = {}
        self._cache_ttl = cache_ttl_hours * 3600

    # ──────────────────────────────────────
    # Grid Lookup (cached)
    # ──────────────────────────────────────

    def _get_grid(self, city: str) -> GridInfo:
        """Resolve lat/lon to NOAA grid office + coordinates."""
        now = time.time()

        if city in self._grid_cache:
            cached = self._grid_cache[city]
            if now - cached.fetched_at < self._cache_ttl:
                return cached

        if city not in CITY_COORDS:
            raise ValueError(f"Unknown city: {city}. Available: {list(CITY_COORDS.keys())}")

        lat, lon = CITY_COORDS[city]
        url = f"{BASE_URL}/points/{lat},{lon}"

        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            props = data["properties"]

            grid = GridInfo(
                office=props["gridId"],
                grid_x=props["gridX"],
                grid_y=props["gridY"],
                fetched_at=now,
            )
            self._grid_cache[city] = grid
            logger.debug(f"Grid for {city}: {grid.office}/{grid.grid_x},{grid.grid_y}")
            return grid

        except requests.RequestException as e:
            logger.error(f"NOAA grid lookup failed for {city}: {e}")
            raise

    # ──────────────────────────────────────
    # Hourly Forecasts
    # ──────────────────────────────────────

    def get_hourly_forecasts(self, city: str) -> List[HourlyForecast]:
        """Fetch hourly forecasts for the next ~7 days."""
        grid = self._get_grid(city)
        url = f"{BASE_URL}/gridpoints/{grid.office}/{grid.grid_x},{grid.grid_y}/forecast/hourly"

        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"NOAA hourly forecast failed for {city}: {e}")
            return []

        forecasts = []
        for period in data.get("properties", {}).get("periods", []):
            pop = None
            if period.get("probabilityOfPrecipitation", {}).get("value") is not None:
                pop = period["probabilityOfPrecipitation"]["value"]

            temp_f = period["temperature"]
            temp_unit = period.get("temperatureUnit", "F")
            if temp_unit == "C":
                temp_c = temp_f
                temp_f = temp_c * 9 / 5 + 32
            else:
                temp_c = (temp_f - 32) * 5 / 9

            forecasts.append(HourlyForecast(
                city=city,
                start_time=datetime.fromisoformat(period["startTime"]),
                end_time=datetime.fromisoformat(period["endTime"]),
                temperature_f=temp_f,
                temperature_c=round(temp_c, 1),
                is_daytime=period.get("isDaytime", True),
                short_forecast=period.get("shortForecast", ""),
                wind_speed=period.get("windSpeed", ""),
                wind_direction=period.get("windDirection", ""),
                probability_of_precipitation=pop,
            ))

        logger.info(f"Fetched {len(forecasts)} hourly periods for {city}")
        return forecasts

    # ──────────────────────────────────────
    # Daily Aggregation
    # ──────────────────────────────────────

    def get_daily_forecasts(self, city: str, days_ahead: int = 3) -> List[DailyForecast]:
        """
        Aggregate hourly forecasts into daily high/low summaries.
        More accurate than the 12h-period endpoint for bucket matching.
        """
        hourly = self.get_hourly_forecasts(city)
        if not hourly:
            return []

        # Group by date
        by_date: Dict[str, List[HourlyForecast]] = {}
        for h in hourly:
            date_str = h.start_time.strftime("%Y-%m-%d")
            by_date.setdefault(date_str, []).append(h)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results = []

        for date_str, periods in sorted(by_date.items()):
            if len(results) >= days_ahead:
                break

            temps_f = [p.temperature_f for p in periods]
            high_f = max(temps_f)
            low_f = min(temps_f)
            high_c = round((high_f - 32) * 5 / 9, 1)
            low_c = round((low_f - 32) * 5 / 9, 1)

            daytime = [p for p in periods if p.is_daytime]
            nighttime = [p for p in periods if not p.is_daytime]

            results.append(DailyForecast(
                city=city,
                date=date_str,
                high_f=high_f,
                low_f=low_f,
                high_c=high_c,
                low_c=low_c,
                daytime_forecast=daytime[0].short_forecast if daytime else "",
                night_forecast=nighttime[0].short_forecast if nighttime else "",
                hourly_temps_f=temps_f,
            ))

        logger.info(f"Built {len(results)} daily forecasts for {city}")
        return results

    # ──────────────────────────────────────
    # Confidence Estimation
    # ──────────────────────────────────────

    @staticmethod
    def estimate_bucket_probability(
        forecast_temp_f: float,
        hourly_temps_f: List[float],
        bucket_low_f: float,
        bucket_high_f: float,
    ) -> float:
        """
        Estimate the probability that the actual temperature falls in
        a given bucket [bucket_low, bucket_high].

        Uses the spread of hourly forecasts as a proxy for uncertainty.
        If NOAA's hourly temps are tightly clustered, confidence is high.

        Returns probability as a float 0.0–1.0.
        """
        if not hourly_temps_f:
            return 0.0

        # Use the forecast high as the point estimate
        point = forecast_temp_f

        # Calculate spread as a measure of forecast uncertainty
        spread = max(hourly_temps_f) - min(hourly_temps_f)

        # Model as a simple triangular/uniform distribution
        # The tighter the spread, the more confidence in the point estimate
        if spread < 4:
            # Tight forecast — high confidence
            # Approximate with a narrow normal-like distribution (σ ≈ 1.5°F)
            sigma = 1.5
        elif spread < 8:
            sigma = 2.5
        elif spread < 12:
            sigma = 3.5
        else:
            sigma = 5.0

        # Simple numerical integration of Gaussian over the bucket
        # P(bucket_low <= T <= bucket_high) where T ~ N(point, sigma²)
        import math

        def phi(x):
            """Standard normal CDF approximation."""
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        z_low = (bucket_low_f - point) / sigma
        z_high = (bucket_high_f - point) / sigma
        probability = phi(z_high) - phi(z_low)

        return round(max(0.0, min(1.0, probability)), 4)
