"""
core/signal_engine.py
=====================
Scans active Order Blocks for retest + confirmation.
Returns at most one signal per cycle (the highest-quality match).
No lookahead — only looks at bars AFTER formation_bar.
"""

import pandas as pd
from datetime import datetime, timezone
from utils.logger import get_logger

log = get_logger("signal_engine")


class SignalEngine:
    """
    For each active OB, checks the CURRENT (last complete) bar for:
      1. Price inside OB zone
      2. Confirmation candle pattern (hammer / shooting star / engulfing)
      3. Trend filter (1H EMA)
      4. Volatility filter (ATR)
      5. Session filter (hour of day)

    Returns a signal dict or None.
    """

    def __init__(self, cfg_signal: dict, cfg_filters: dict,
                 cfg_risk: dict, pip_size: float = 0.0001):
        self.cs  = cfg_signal
        self.cf  = cfg_filters
        self.cr  = cfg_risk
        self.pip = pip_size

    # ── public ────────────────────────────────

    def evaluate(self, df: pd.DataFrame, obs: list[dict],
                 symbol: str = "EURUSD", fired_signals: set | None = None) -> list[dict]:
        """
        Called once per main loop cycle with the latest bar data.
        Returns ALL valid signals found (may be multiple from different OBs).
        Returns empty list if none found.
        
        fired_signals: set of (ob_index, ob_type) tuples that already generated signals
        """
        signals = []
        
        if len(df) < 10:
            return signals

        # Filters that apply to the entire cycle
        if not self._session_ok(df):
            return signals

        # Use the LAST COMPLETED bar (index -2 to avoid partial bar)
        i = len(df) - 2

        for ob in obs:
            if not ob['active']:
                continue

            # Must be retesting AFTER impulse finished
            min_bar = ob['formation_bar'] + self.cf.get('min_retest_gap', 5)
            if i < min_bar:
                continue
            
            # Skip if this OB already fired a signal in a previous cycle
            if fired_signals and (ob['bar_index'], ob['type']) in fired_signals:
                log.debug(f"OB@{ob['bar_index']} ({ob['type']}): already fired signal — skipping")
                continue

            sig = self._check_ob(df, ob, i, symbol)
            if sig:
                signals.append(sig)

        return signals

    # ── private ───────────────────────────────

    def _check_ob(self, df, ob, i, symbol) -> dict | None:
        price = df['Close'].iloc[i]
        lo    = df['Low'].iloc[i]
        hi    = df['High'].iloc[i]
        o     = df['Open'].iloc[i]

        # 1. Is price retesting the OB zone?
        in_zone = False
        if ob['type'] == 'Bullish':
            if lo <= ob['top'] and price >= ob['bottom']:
                in_zone = True
        else:
            if hi >= ob['bottom'] and price <= ob['top']:
                in_zone = True

        if not in_zone:
            # Invalidate if price closed through
            if ob['type'] == 'Bullish' and price < ob['bottom']:
                ob['active'] = False
                ob['mitigated'] = True
            elif ob['type'] == 'Bearish' and price > ob['top']:
                ob['active'] = False
                ob['mitigated'] = True
            return None

        # Early exit: prevent duplicate signals on same bar
        if ob.get('_signal_fired_bar') == i:
            return None

        # 2. Confirmation candle
        confirmed, sig_type = self._confirmation(df, i, ob['type'])
        if not confirmed:
            log.debug(f"OB@{ob['formation_bar']}: confirmation candle rejected (no hammer/engulf)")
            return None

        # 3. Trend filter
        if not self._trend_ok(df, i, sig_type):
            price = df['Close'].iloc[i]
            ema = df['EMA_1H'].iloc[i]
            log.warning(f"OB@{ob['formation_bar']}: EMA trend filter rejected {sig_type} (price={price:.5f}, EMA={ema:.5f})")
            return None

        # 4. Volatility filter
        if not self._volatility_ok(df, i):
            atr_now = df['ATR'].iloc[i]
            atr_mean = df['ATR'].mean()
            log.warning(f"OB@{ob['formation_bar']}: ATR volatility filter rejected (ATR={atr_now:.4f}, mean={atr_mean:.4f}, min={atr_mean*0.5:.4f})")
            return None

        # 5. Build signal
        buf  = self.cr.get('sl_buffer_pips', 3) * self.pip
        spr  = 1 * self.pip  # 1-pip spread
        entry = price + spr if sig_type == 'BUY' else price - spr

        if sig_type == 'BUY':
            sl   = ob['bottom'] - buf
            risk = entry - sl
            tp1  = entry + risk * self.cr.get('tp1_rr', 1.0)
            tp2  = entry + risk * self.cr.get('tp2_rr', 2.0)
        else:
            sl   = ob['top'] + buf
            risk = sl - entry
            tp1  = entry - risk * self.cr.get('tp1_rr', 1.0)
            tp2  = entry - risk * self.cr.get('tp2_rr', 2.0)

        if risk <= 0:
            return None

        # Calculate risk in pips correctly
        risk_pips = round(risk / self.pip, 1)
        
        # Validate TP levels
        if sig_type == 'BUY':
            if tp1 >= tp2:
                log.warning(f"Invalid TP levels for BUY: TP1={tp1:.5f} >= TP2={tp2:.5f}")
                return None
        else:  # SELL
            if tp1 <= tp2:
                log.warning(f"Invalid TP levels for SELL: TP1={tp1:.5f} <= TP2={tp2:.5f}")
                return None

        ob['active'] = False  # prevent duplicate signals on the same OB
        ob['_signal_fired_bar'] = i  # Track which bar fired the signal

        signal = {
            'symbol':    symbol,
            'timestamp': df.index[i],
            'bar_index': i,
            'type':      sig_type,
            'entry':     round(entry, 5),
            'sl':        round(sl, 5),
            'tp1':       round(tp1, 5),
            'tp2':       round(tp2, 5),
            'risk_pips': risk_pips,
            'ob_type':   ob['type'],
            'ob_top':    ob['top'],
            'ob_bottom': ob['bottom'],
            'ob_bar':    ob['bar_index'],
        }

        log.info(
            f"SIGNAL | {sig_type} {symbol} | "
            f"Entry={entry:.5f} SL={sl:.5f} TP1={tp1:.5f} TP2={tp2:.5f} | "
            f"Risk={risk_pips:.1f}pips"
        )
        return signal

    def _confirmation(self, df, i, ob_type) -> tuple[bool, str | None]:
        o = df['Open'].iloc[i]
        h = df['High'].iloc[i]
        l = df['Low'].iloc[i]
        c = df['Close'].iloc[i]
        body = abs(c - o)

        if ob_type == 'Bullish':
            # Hammer / pin bar: lower wick > body × ratio
            lower_wick = min(o, c) - l
            if body > 0 and lower_wick >= body * self.cs.get('wick_ratio', 1.5):
                return True, 'BUY'
            # Bullish engulfing
            if self.cs.get('engulf', True) and i > 0:
                prev_o = df['Open'].iloc[i - 1]
                prev_c = df['Close'].iloc[i - 1]
                if (prev_c < prev_o and c > o and
                        c > prev_o and o < prev_c):
                    return True, 'BUY'

        else:  # Bearish OB
            # Shooting star: upper wick > body × ratio
            upper_wick = h - max(o, c)
            if body > 0 and upper_wick >= body * self.cs.get('wick_ratio', 1.5):
                return True, 'SELL'
            # Bearish engulfing
            if self.cs.get('engulf', True) and i > 0:
                prev_o = df['Open'].iloc[i - 1]
                prev_c = df['Close'].iloc[i - 1]
                if (prev_c > prev_o and c < o and
                        c < prev_o and o > prev_c):
                    return True, 'SELL'

        return False, None

    def _trend_ok(self, df, i, sig_type) -> bool:
        if not self.cf.get('use_ema_filter', True):
            return True
        price = df['Close'].iloc[i]
        ema   = df['EMA_1H'].iloc[i]
        if sig_type == 'BUY':
            return price > ema
        return price < ema

    def _volatility_ok(self, df, i) -> bool:
        mult = self.cf.get('min_atr_multiplier', 0.5)
        atr_now  = df['ATR'].iloc[i]
        atr_mean = df['ATR'].mean()
        return atr_now >= mult * atr_mean

    def _session_ok(self, df) -> bool:
        start = self.cf.get('session_start_utc')
        end   = self.cf.get('session_end_utc')
        if start is None or end is None:
            return True
        hour = df.index[-1].hour
        return start <= hour < end
