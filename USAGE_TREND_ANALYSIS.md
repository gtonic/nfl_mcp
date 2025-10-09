# Usage Trend Analysis Feature

## Overview

The usage trend analysis feature automatically calculates whether a player's usage is increasing, decreasing, or staying flat over recent weeks. This helps identify breakout candidates and fading players.

## How It Works

### Calculation Method

1. **Get Weekly Data**: Retrieves individual week stats for the last 3 weeks
2. **Compare Recent vs Prior**: Compares most recent week to average of prior weeks
3. **Calculate % Change**: `(most_recent - prior_avg) / prior_avg * 100`
4. **Apply Threshold**: 15% change required to mark as trending

### Trend Values

- **"up"** (↑): Most recent week is >15% higher than prior average
- **"down"** (↓): Most recent week is >15% lower than prior average  
- **"flat"** (→): Change is within ±15%

### Example

```python
# Player has these target counts:
Week 3: 6 targets
Week 4: 8 targets  
Week 5: 12 targets

# Calculation:
prior_avg = (6 + 8) / 2 = 7
most_recent = 12
pct_change = (12 - 7) / 7 * 100 = 71%

# Result: "up" (71% > 15%)
```

## Enrichment Fields

### usage_trend

Object containing trend for individual metrics:

```json
{
  "usage_trend": {
    "targets": "up",      // Receiving targets trend
    "routes": "flat",     // Routes run trend
    "snap_share": "up"    // Snap percentage trend
  }
}
```

### usage_trend_overall

Single trend value representing overall player trajectory:

```json
{
  "usage_trend_overall": "up"
}
```

**Priority Logic:**
1. If targets available → use targets trend (most important for pass catchers)
2. Else if snap_share available → use snap_share trend (volume indicator)
3. Else if routes available → use routes trend (opportunity indicator)

## API Response Example

```json
{
  "player_id": "4046",
  "full_name": "CeeDee Lamb",
  "position": "WR",
  "usage_last_3_weeks": {
    "targets_avg": 11.3,
    "routes_avg": 35.7,
    "rz_touches_avg": 2.0,
    "snap_share_avg": 91.5,
    "weeks_sample": 3
  },
  "usage_trend": {
    "targets": "up",
    "routes": "flat", 
    "snap_share": "up"
  },
  "usage_trend_overall": "up",
  "usage_source": "sleeper"
}
```

## Use Cases

### 1. Waiver Wire Targets

Identify trending-up players before they become widely owned:

```
Filter: usage_trend_overall == "up" AND targets_avg > 6
```

**Why it works:** Rising target share indicates increased role in offense

### 2. Trade Candidates

Find players to sell high (trending down):

```
Filter: usage_trend_overall == "down" AND points_last_3 > 15
```

**Why it works:** Good recent stats but declining usage = sell before crash

### 3. Breakout Detection

Spot emerging stars:

```
Filter: 
  - usage_trend_overall == "up"
  - snap_share_avg > 70%
  - targets_avg increased by >30% week-over-week
```

### 4. Injury Risk Assessment

Declining snap share may indicate injury:

```
Filter: snap_share trend == "down" AND practice_status == "LP"
```

## Trend Interpretation Guide

### For Wide Receivers

| Trend | Targets | Routes | Interpretation |
|-------|---------|--------|----------------|
| up | up | up | **Elite target**: Clear WR1 role emerging |
| up | up | flat | **Efficiency boost**: Same snaps, more targets |
| up | flat | down | **Red flag**: Fewer opportunities, unsustainable |
| down | down | down | **Fading role**: Losing snaps and targets |
| flat | flat | flat | **Steady state**: Established role, predictable |

### For Running Backs

| Trend | Touches | Snap Share | Interpretation |
|-------|---------|------------|----------------|
| up | up | up | **Bell cow emerging**: Increasing workload |
| up | up | flat | **Goal line role**: More touches in same snaps |
| flat | up | down | **Pass-catching back**: Targets up, rushes down |
| down | down | down | **Committee backfield**: Losing touches |

### For Tight Ends

| Trend | Targets | Red Zone | Interpretation |
|-------|---------|----------|----------------|
| up | up | up | **TE1 upside**: High-volume, scoring opportunities |
| up | up | flat | **Safety valve**: More targets, limited TD upside |
| flat | down | up | **TD-dependent**: Scoring chances but low volume |
| down | down | down | **Streaming option**: Inconsistent usage |

## Configuration

### Threshold Adjustment

The 15% threshold is configurable in `sleeper_tools.py`:

```python
def _calculate_usage_trend(weekly_data, metric):
    # ...
    THRESHOLD = 15  # Adjust here
    
    if pct_change > THRESHOLD:
        return "up"
    elif pct_change < -THRESHOLD:
        return "down"
    else:
        return "flat"
```

**Recommended Values:**
- **Conservative (20%)**: Fewer false positives, catch only major shifts
- **Standard (15%)**: Balanced detection of meaningful trends
- **Aggressive (10%)**: More sensitive, catch emerging trends earlier

### Minimum Sample Size

Requires at least 2 weeks of data for trend calculation. With 1 week, returns `None`.

## Limitations

1. **Small Sample Size**: 3 weeks may not capture true trends
2. **Injury Impact**: Sudden changes due to injuries may not be "trends"
3. **Opponent Strength**: Usage varies by game script and opponent
4. **Position Differences**: RB usage more volatile than WR
5. **Bye Weeks**: Gaps in data affect calculations

## Advanced Usage

### Weighted Trend

For more sophisticated analysis, weight recent weeks more heavily:

```python
# Week 5: weight 3x
# Week 4: weight 2x  
# Week 3: weight 1x

weighted_avg = (week5*3 + week4*2 + week3*1) / 6
```

### Multi-Metric Scoring

Combine trends into single score:

```python
score = 0
if usage_trend["targets"] == "up": score += 3
if usage_trend["routes"] == "up": score += 2
if usage_trend["snap_share"] == "up": score += 2

# Score 7 = elite upward trend
# Score 0 = no upward momentum
```

### Correlation Analysis

Track correlation between snap share and production:

```python
if snap_share == "up" and points == "up":
    confidence = "high"  # Usage driving production
elif snap_share == "flat" and points == "up":
    confidence = "medium"  # Efficiency driving production (less sustainable)
```

## Testing

The trend calculation is thoroughly tested:

```bash
# Run trend tests
python -m pytest tests/test_usage_trend.py -v

# Run integration tests
python -m pytest tests/test_usage_integration.py -v
```

Test coverage includes:
- Upward, downward, flat trends
- None/null value handling
- Insufficient data scenarios
- Zero baseline handling
- Multiple metrics (targets, routes, snap_share)

## Future Enhancements

Potential improvements:

1. **Strength of Schedule Adjustment**: Factor in opponent difficulty
2. **Game Script Analysis**: Account for positive/negative game scripts
3. **Volatility Metrics**: Track consistency alongside trend
4. **Injury-Adjusted Trends**: Exclude games with limited snaps due to injury
5. **Position-Specific Thresholds**: Different thresholds for RB vs WR
6. **Historical Comparison**: Compare to player's season-long average

## References

- Database: `nfl_mcp/database.py` - `get_usage_weekly_breakdown()`
- Calculation: `nfl_mcp/sleeper_tools.py` - `_calculate_usage_trend()`
- Enrichment: `nfl_mcp/sleeper_tools.py` - `_enrich_usage_and_opponent()`
- Tests: `tests/test_usage_trend.py`, `tests/test_usage_integration.py`
- API Docs: `API_DOCS.md` - Schema v8 enrichment fields
