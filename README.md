# Polymarket Weather Trading Bot

A standalone Python bot that exploits mispricings between NOAA weather forecasts and Polymarket temperature bucket markets.

## How It Works

```
NOAA API (free)          Polymarket CLOB API
     │                         │
     ▼                         ▼
┌─────────────┐       ┌──────────────────┐
│  Hourly     │       │  Weather bucket  │
│  temperature│       │  markets + prices│
│  forecasts  │       │  (e.g., "NYC     │
│  (7 days)   │       │   72-74°F = 11¢")│
└──────┬──────┘       └────────┬─────────┘
       │                       │
       └───────────┬───────────┘
                   ▼
          ┌────────────────┐
          │    SCANNER     │
          │                │
          │  For each city │
          │  + date:       │
          │                │
          │  1. Get NOAA   │
          │     forecast   │
          │  2. Estimate   │
          │     bucket     │
          │     probability│
          │  3. Compare to │
          │     market     │
          │     price      │
          │  4. If edge >  │
          │     threshold  │
          │     → SIGNAL   │
          └───────┬────────┘
                  │
          ┌───────▼────────┐
          │   SAFEGUARDS   │
          │                │
          │  • Slippage    │
          │  • Flip-flop   │
          │  • Time decay  │
          │  • Liquidity   │
          └───────┬────────┘
                  │
          ┌───────▼────────┐
          │   EXECUTOR     │
          │                │
          │  Market order  │
          │  via py-clob   │
          │  on Polygon    │
          └───────┬────────┘
                  │
                  ▼
          Telegram notification
```

## The Edge

NOAA's 24-48 hour temperature forecasts are accurate ~85-90% of the time. Polymarket's temperature bucket markets are often priced by retail users without access to (or awareness of) NOAA's granular forecasts.

When NOAA estimates 90% probability for a bucket priced at 11¢, the bot buys. When the market corrects toward fair value (45¢+), the bot sells. That's a 3-4x return on a high-probability trade.

**Important caveats:**
- The edge is compressing as more bots enter
- NOAA is wrong 10-15% of the time — losses are expected
- Liquidity is thin — slippage eats into returns
- This is real money on a prediction market with real risk

## Project Structure

```
weather_bot/
├── main.py              # CLI entrypoint + bot controller
├── config.py            # All configurable parameters
├── noaa_client.py       # NOAA API client + probability estimation
├── polymarket_client.py # Gamma API (discovery) + CLOB API (trading)
├── scanner.py           # Core engine: match forecasts to buckets
├── notifier.py          # Telegram notifications
├── requirements.txt     # Dependencies
├── .env.example         # Environment variable template
└── README.md
```

## Quick Start

### 1. Install

```bash
git clone <this-repo>
cd weather_bot
pip install -r requirements.txt
```

### 2. Scan Markets (Read-Only)

No API keys needed — this just shows you what opportunities exist:

```bash
# Single scan with verbose output
python main.py scan --once --verbose

# Continuous scanning every 2 minutes
python main.py scan --interval 120

# Filter to specific cities
python main.py scan --once --cities NYC,Chicago,Miami
```

### 3. Dry Run (Simulated Trading)

Runs the full trading logic but only logs what it would do:

```bash
python main.py trade --dry-run --verbose
```

### 4. Live Trading

```bash
# Set up your keys
cp .env.example .env
# Edit .env with your Polymarket private key + funder address

# Install the trading client
pip install py-clob-client

# Go live
python main.py trade
```

## Configuration

All parameters are in `config.py`. Key settings:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entry_threshold` | 0.15 | Only buy buckets priced below 15¢ |
| `exit_threshold` | 0.45 | Sell when price corrects above 45¢ |
| `min_edge` | 0.20 | Minimum NOAA − market spread to act |
| `max_position_usd` | 2.00 | Max $ per single trade |
| `max_total_exposure` | 100.00 | Max aggregate $ across all positions |
| `scan_interval_seconds` | 120 | Scan frequency (2 min) |
| `max_trades_per_scan` | 5 | Cap trades per cycle |
| `min_hours_to_resolution` | 2 | Skip markets resolving within 2h |
| `max_slippage_pct` | 0.15 | Abort if slippage exceeds 15% |

Override any parameter via CLI flags:
```bash
python main.py scan --once \
  --entry-threshold 0.10 \
  --exit-threshold 0.50 \
  --max-position 5.0 \
  --min-edge 0.15
```

## Probability Estimation

Since NOAA doesn't output bucket probabilities directly, we estimate them:

1. **Fetch hourly forecasts** for the target city + date
2. **Calculate forecast spread** (max hourly temp − min hourly temp)
3. **Map spread to uncertainty** (tight spread = high confidence, σ ≈ 1.5°F; wide spread = low confidence, σ ≈ 5°F)
4. **Integrate Gaussian** over the bucket range to get P(bucket_low ≤ T ≤ bucket_high)

This is a simplification. For better accuracy, consider:
- Using NOAA's `forecastGridData` endpoint for explicit probability distributions
- Incorporating ensemble model data (GFS, NAM, HRRR)
- Tracking historical NOAA accuracy by city/season
- Adding a calibration layer based on observed vs forecast outcomes

## Telegram Notifications

Set up for remote monitoring:

```bash
# In .env:
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
```

You'll get notifications for:
- Bot startup with config summary
- Each scan with opportunities found
- Trade executions with details
- Errors and safeguard blocks

## Extending the Bot

### Add New Cities

In `config.py`, add to `CITY_COORDS`:
```python
CITY_COORDS["Denver"] = (39.7392, -104.9903)
```

### Custom Strategies

Subclass or modify `WeatherScanner._evaluate_signal()` to implement:
- **Contrarian exits** — sell into market overreactions
- **Multi-bucket hedging** — buy adjacent buckets to reduce variance
- **Momentum overlay** — track price trends over the last N hours
- **Cross-city correlation** — if NYC is cold, Chicago likely is too

### Backtesting

The architecture supports backtesting by:
1. Saving all NOAA forecasts + Polymarket snapshots to CSV/SQLite
2. Replaying through the scanner with historical data
3. Tracking simulated P&L

This is left as an exercise — the scanner's `scan()` method can be fed mock data.

## Known Limitations

- **No portfolio tracking**: The bot doesn't currently track cumulative P&L or position inventory. Add a SQLite layer for this.
- **Single-threaded**: Scans are sequential. For faster cycles across many cities, add async/threading.
- **No WebSocket**: Uses REST polling. Polymarket offers WebSocket feeds for real-time price updates.
- **Simple probability model**: The Gaussian approximation works but isn't calibrated. A proper model would train on historical forecast error distributions.
- **US cities only**: NOAA covers the US. For international markets (London, Seoul), use Open-Meteo or other providers.

## Regulatory

Polymarket is unavailable in 33+ countries including the US, UK, France, Germany, Australia, and others. Check your jurisdiction before trading. This code is for educational purposes.

## License

MIT — use at your own risk. Not financial advice.
