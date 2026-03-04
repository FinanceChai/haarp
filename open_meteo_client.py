"""
Open-Meteo Forecast Client
============================
Fetches daily and hourly forecasts from the Open-Meteo API.
Free, no API key required, global coverage.

Returns the same DailyForecast dataclass as noaa_client.py so
the scanner can use either source transparently.
"""

import logging
from typing import Dict, List

import requests

from config import CITY_COORDS
from noaa_client import DailyForecast

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoClient:
    """Fetches forecasts from Open-Meteo for international cities."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolyWeatherBot/1.0",
            "Accept": "application/json",
        })

    def get_daily_forecasts(self, city: str, days_ahead: int = 3) -> List[DailyForecast]:
        """
        Fetch daily forecasts from Open-Meteo.

        Requests both daily high/low and hourly temps in Fahrenheit.
        Groups hourly data by date for the Gaussian bucket probability estimation.
        """
        if city not in CITY_COORDS:
            raise ValueError(f"Unknown city: {city}. Available: {list(CITY_COORDS.keys())}")

        lat, lon = CITY_COORDS[city]

        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "forecast_days": min(days_ahead + 1, 7),
            "timezone": "auto",
        }

        try:
            resp = self.session.get(BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Open-Meteo fetch failed for {city}: {e}")
            return []

        return self._parse_response(data, city, days_ahead)

    def _parse_response(self, data: dict, city: str, days_ahead: int) -> List[DailyForecast]:
        """Parse Open-Meteo JSON into DailyForecast objects."""
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        dates = daily.get("time", [])
        highs_f = daily.get("temperature_2m_max", [])
        lows_f = daily.get("temperature_2m_min", [])

        hourly_times = hourly.get("time", [])
        hourly_temps = hourly.get("temperature_2m", [])

        # Group hourly temps by date
        hourly_by_date: Dict[str, List[float]] = {}
        for t_str, temp in zip(hourly_times, hourly_temps):
            if temp is None:
                continue
            date_part = t_str[:10]  # "2026-03-04T14:00" → "2026-03-04"
            hourly_by_date.setdefault(date_part, []).append(temp)

        results = []
        for i, date_str in enumerate(dates):
            if len(results) >= days_ahead:
                break
            if i >= len(highs_f) or i >= len(lows_f):
                break

            high_f = highs_f[i]
            low_f = lows_f[i]
            if high_f is None or low_f is None:
                continue

            high_c = round((high_f - 32) * 5 / 9, 1)
            low_c = round((low_f - 32) * 5 / 9, 1)

            results.append(DailyForecast(
                city=city,
                date=date_str,
                high_f=high_f,
                low_f=low_f,
                high_c=high_c,
                low_c=low_c,
                daytime_forecast="",
                night_forecast="",
                hourly_temps_f=hourly_by_date.get(date_str, []),
            ))

        logger.info(f"Open-Meteo: {len(results)} daily forecasts for {city}")
        return results
