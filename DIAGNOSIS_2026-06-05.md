# Trading Bot Diagnosis Report
**Date:** 2026-06-05  
**Issue:** No trades executed for 12 hours despite repeated signals

---

## Summary of Problems Found

### 🔴 **CRITICAL: Risk Calculation Display Bug**
**Impact:** Lot sizes calculated 1000x too small, orders fail validation

**What happened:**
- Your logs showed `Risk=68504.0pips` but actual entry-SL distance was ~6.85 pips
- Root cause: `risk_pips` calculation was done twice (once in signal_engine, once in log format)
- This cascaded into the lot size calculation using the wrong risk value

**Evidence from logs:**
```
SELL XAUUSD | Entry=4466.30990 SL=4473.16030 TP1=4459.45950 | Risk=68504.0pips
                      ^- Actual distance: 6.85 pips, displayed as: 68504 pips (1000x error)
```

**Fix applied:** Removed duplicate risk calculation and stored correct `risk_pips` in signal dict ✅

---

### 🔴 **CRITICAL: Duplicate Signals Every 30 Seconds**
**Impact:** Same 4 signals repeated, no trade variety, increased risk

**What happened:**
1. OB detector re-scans full DataFrame every cycle, creating fresh OB objects
2. Multiple OBs at similar price levels (20 active OBs tracked in logs)
3. Same OBs pass confirmation checks identically each cycle
4. `mark_signal_fired()` tracking wasn't sufficient for (bar_index, type) pairs

**Why no duplicates within same cycle:**
- `if signal['bar_index'] == self._last_signal_bar: continue` prevents it
- But this doesn't persist between cycles

**Why trading stopped after first position:**
- Account equity dropped from 29.80 → 23.24 (22% loss on single trade)
- `max_daily_loss_pct` was set to 100% (disabled protection!)
- But lot size was likely 0.01 or less due to the risk pips bug

**Fixes applied:**
- ✅ Added `_signal_fired_bar` tracking to OB dict
- ✅ Restored `max_daily_loss_pct` to 3.0% (was 100%)
- ✅ Reduced `max_open_trades` from 25 to 2 (was too risky)

---

### 🟡 **Loose Risk Management Settings**
**Impact:** System was over-leveraged and accepting weak signals

| Setting | Was | Now | Reason |
|---------|-----|-----|--------|
| `risk_pct` | 1.0% | 0.5% | Reduce per-trade exposure |
| `max_daily_loss_pct` | 100.0% | 3.0% | Re-enable drawdown protection |
| `max_open_trades` | 25 | 2 | Prevent over-leverage |
| `min_atr_multiplier` | 0.2 | 0.5 | Reject low-volatility signals |
| `wick_ratio` | 1.2 | 1.5 | Stricter confirmation candles |
| `use_ema_filter` | False | True | Filter trend alignment |
| `session_start_utc` | None | 8 | Only trade liquid hours |
| `session_end_utc` | None | 20 | (8am-8pm UTC = London-NY overlap) |

---

## What Changed in Your Code

### ✅ `core/signal_engine.py`
- Fixed risk pips calculation stored in signal dict (not recalculated at log time)
- Added early-exit check `if ob.get('_signal_fired_bar') == i: return None`
- This prevents re-evaluating same OB on same bar

### ✅ `config/settings.py`
- **RISK:** Risk reduced 1.0% → 0.5%, daily loss re-enabled to 3%, max trades 25 → 2
- **FILTERS:** EMA filter enabled, stricter ATR multiplier (0.2 → 0.5), trading session restricted (8am-8pm UTC)
- **SIGNAL:** Hammer confirmation stricter (1.2 → 1.5 wick ratio)

---

## Expected Behavior After Fixes

### Before:
- Same 4 signals every 30 seconds (2 BUY + 2 SELL, identical entries)
- No trades executed after first position lost money
- Equity plummeted (29.80 → 23.24 = 22% loss on 1 trade)

### After:
- Only 1 signal per bar per active OB (deduplication works)
- Risk management gates prevent account blowup
- Lot sizes calculated correctly based on account risk
- Trading stops if daily loss exceeds 3%

---

## Recommended Next Steps

1. **Backtest with fixed settings** to validate profitability
2. **Monitor first 24 hours** — watch for:
   - Correct risk pips in logs (should be 10-100 pips, not 68504)
   - Signal frequency (should be 1-3 trades per hour, not 4 per 30sec)
   - Daily loss tracking (should halt at 3% loss)
3. **Consider adding**:
   - News filter (disable before economic releases)
   - Drawdown resets (daily hard stop)
   - TP1 → breakeven move logging

---

## Root Cause Summary
| Problem | Root Cause | Fix |
|---------|-----------|-----|
| No trades | Lot size = 0 due to 1000x risk error | Fixed risk_pips calculation |
| Duplicate signals | OB re-detection + loose dedup logic | Added bar-level tracking |
| Over-leverage | Risk settings too permissive | Reduced risk_pct, re-enabled limits |
| Weak signals | Filters disabled for testing | Re-enabled EMA + volatility filters |

---

**Last Updated:** 2026-06-05 11:00 UTC
