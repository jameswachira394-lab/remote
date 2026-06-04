"""
config/settings.py
==================
Central configuration for the OB trading system.
Edit this file before running. All other modules import from here.
"""

# ══════════════════════════════════════════════
# MT5 CONNECTION
# ══════════════════════════════════════════════
MT5 = {
    "path": r"C:\Program Files\MetaTrader 5\terminal64.exe",  # adjust to your install
    "login": 10401216,             # your MT5 account number (int)
    "password": ":erBo0B{",         # your MT5 password
    "server": "FBS-Demo",           # broker server, e.g. "ICMarkets-Demo"
    "timeout": 10_000,      # connection timeout ms
}

# ══════════════════════════════════════════════
# INSTRUMENT
# ══════════════════════════════════════════════
SYMBOL      = "XAUUSD"      # MT5 symbol name (check your broker's exact spelling)
TIMEFRAME   = "M5"          # M1 M5 M15 M30 H1 H4 D1
BARS        = 1500          # How many bars to load on each cycle

# ══════════════════════════════════════════════
# ORDER BLOCK DETECTION
# ══════════════════════════════════════════════
OB = {
    "impulse_candles":  3,   # Consecutive same-direction candles for impulse
    "bos_lookback":     5,   # Swing lookback for BOS confirmation
    "min_retest_gap":   5,   # Minimum bars between OB formation and retest
    "max_ob_age_bars":  300, # Discard OBs older than this many bars
}

# ══════════════════════════════════════════════
# SIGNAL / ENTRY
# ══════════════════════════════════════════════
SIGNAL = {
    "wick_ratio":     1.2,   # Lower wick / body ratio for hammer confirmation (reduced)
    "engulf":         false,  # Also accept engulfing candles as confirmation
}

# ══════════════════════════════════════════════
# RISK MANAGEMENT
# ══════════════════════════════════════════════
RISK = {
    "risk_pct":          1.0,   # % of account balance to risk per trade
    "sl_buffer_pips":    3,     # Extra pips beyond OB boundary for SL
    "tp1_rr":            1.0,   # TP1 reward:risk ratio
    "tp2_rr":            2.0,   # TP2 reward:risk ratio
    "max_open_trades":   25,     # Max concurrent open positions
    "max_daily_loss_pct":3.0,   # Halt trading if daily drawdown exceeds this %
    "move_be_at_tp1":    True,  # Move SL to breakeven after TP1 hit
    "trailing_stop_enabled": False,  # Enable trailing stop (DISABLED - causing losses)
    "trailing_stop_pips": 15,   # Trailing stop distance in pips from current price
}

# ══════════════════════════════════════════════
# FILTERS
# ══════════════════════════════════════════════
FILTERS = {
    "ema_1h_period":      200,   # 50-period × 12 bars ≈ 1H EMA on 5m data
    "atr_period":         14,
    "min_atr_multiplier": 0.2,   # Signal ATR must be ≥ X × mean ATR (reduced from 0.5)
    "use_ema_filter":     False, # Disable EMA trend filter for testing
    # Trading sessions (UTC hours, inclusive) — set both to None to disable
    "session_start_utc":  None,  # Disabled for testing
    "session_end_utc":    None,  # Disabled for testing
    "skip_news":          False, # Set True to enable news filter (requires API key)
}

# ══════════════════════════════════════════════
# EXECUTION
# ══════════════════════════════════════════════
EXEC = {
    "magic_number":   202501,   # Unique ID for this bot's orders
    "comment":        "OB_Bot", # Order comment shown in MT5
    "slippage":       3,        # Max slippage in points
    "retry_attempts": 3,        # Retry failed orders N times
    "retry_delay_s":  1.0,      # Seconds between retries
    "pip_size":       0.0001,   # EUR/USD; use 0.01 for JPY pairs
}

# ══════════════════════════════════════════════
# LOOP TIMING
# ══════════════════════════════════════════════
LOOP = {
    "cycle_seconds":    30,   # Main loop interval (check for signals every N sec)
    "position_check_s": 10,   # How often to check open positions for BE move
}

# ══════════════════════════════════════════════
# TELEGRAM ALERTS (optional)
# ══════════════════════════════════════════════
TELEGRAM = {
    "enabled":   False,
    "token":     "",          # BotFather token
    "chat_id":   "",          # Your chat ID
}

# ══════════════════════════════════════════════
# PATHS
# ══════════════════════════════════════════════
import os
BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR    = os.path.join(BASE_DIR, "logs")
DATA_DIR   = os.path.join(BASE_DIR, "data")
CHART_DIR  = os.path.join(BASE_DIR, "charts")

for _d in (LOG_DIR, DATA_DIR, CHART_DIR):
    os.makedirs(_d, exist_ok=True)
