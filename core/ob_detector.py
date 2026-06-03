"""
core/ob_detector.py
===================
Detects institutional Order Blocks on a DataFrame of OHLCV bars.
No lookahead — detection fires only after the impulse is fully closed.
"""

import pandas as pd
from utils.logger import get_logger

log = get_logger("ob_detector")


class OrderBlockDetector:
    """
    Scans a DataFrame and returns a list of active OB dicts.

    Each OB dict contains:
        type          : 'Bullish' | 'Bearish'
        top           : float  (OB high)
        bottom        : float  (OB low)
        bar_index     : int    (index in df)
        timestamp     : pd.Timestamp
        formation_bar : int    (bar at which OB was confirmed — no lookahead before this)
        active        : bool   (False once price trades through)
        mitigated     : bool   (True once fully broken)
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def detect(self, df: pd.DataFrame) -> list[dict]:
        n_imp = self.cfg.get("impulse_candles", 3)
        lb    = self.cfg.get("bos_lookback",    5)
        max_age = self.cfg.get("max_ob_age_bars", 300)

        obs          = []
        seen_indices = set()
        last_bar     = len(df) - 1
        cutoff       = last_bar - max_age  # discard very old OBs

        for i in range(lb, last_bar - n_imp - 1):
            # ── Bullish impulse → Bullish OB ──────────────────
            if self._is_impulse(df, i, 'bullish', n_imp, lb):
                for j in range(i, max(i - lb - 1, -1), -1):
                    if self._is_bearish(df, j) and j not in seen_indices and j >= cutoff:
                        obs.append({
                            'type':          'Bullish',
                            'top':           df['High'].iloc[j],
                            'bottom':        df['Low'].iloc[j],
                            'bar_index':     j,
                            'timestamp':     df.index[j],
                            'formation_bar': i + n_imp,
                            'active':        True,
                            'mitigated':     False,
                        })
                        seen_indices.add(j)
                        break

            # ── Bearish impulse → Bearish OB ──────────────────
            if self._is_impulse(df, i, 'bearish', n_imp, lb):
                for j in range(i, max(i - lb - 1, -1), -1):
                    if self._is_bullish(df, j) and j not in seen_indices and j >= cutoff:
                        obs.append({
                            'type':          'Bearish',
                            'top':           df['High'].iloc[j],
                            'bottom':        df['Low'].iloc[j],
                            'bar_index':     j,
                            'timestamp':     df.index[j],
                            'formation_bar': i + n_imp,
                            'active':        True,
                            'mitigated':     False,
                        })
                        seen_indices.add(j)
                        break

        # Mark mitigated OBs (price fully traded through)
        self._mark_mitigated(df, obs)

        active = [o for o in obs if o['active']]
        log.debug(
            f"OBs detected: {len(obs)} total | {len(active)} active "
            f"({sum(1 for o in active if o['type']=='Bullish')} bull / "
            f"{sum(1 for o in active if o['type']=='Bearish')} bear)"
        )
        return obs

    # ── private ───────────────────────────────

    def _is_bullish(self, df, i):
        return df['Close'].iloc[i] > df['Open'].iloc[i]

    def _is_bearish(self, df, i):
        return df['Close'].iloc[i] < df['Open'].iloc[i]

    def _is_impulse(self, df, i: int, direction: str,
                    n_imp: int, lb: int) -> bool:
        if i + n_imp >= len(df):
            return False

        if direction == 'bullish':
            all_same = all(
                df['Close'].iloc[i + k] > df['Open'].iloc[i + k]
                for k in range(1, n_imp + 1)
            )
            if not all_same:
                return False
            prior_high = df['High'].iloc[max(0, i - lb): i].max()
            return df['Close'].iloc[i + n_imp] > prior_high

        else:  # bearish
            all_same = all(
                df['Close'].iloc[i + k] < df['Open'].iloc[i + k]
                for k in range(1, n_imp + 1)
            )
            if not all_same:
                return False
            prior_low = df['Low'].iloc[max(0, i - lb): i].min()
            return df['Close'].iloc[i + n_imp] < prior_low

    def _mark_mitigated(self, df: pd.DataFrame, obs: list):
        """
        Mark OBs as inactive/mitigated when price has fully traded through.
        Called once after bulk detection on the current bar snapshot.
        """
        last_close = df['Close'].iloc[-1]
        last_low   = df['Low'].iloc[-1]
        last_high  = df['High'].iloc[-1]

        for ob in obs:
            if not ob['active']:
                continue
            if ob['type'] == 'Bullish' and last_close < ob['bottom']:
                ob['active']    = False
                ob['mitigated'] = True
            elif ob['type'] == 'Bearish' and last_close > ob['top']:
                ob['active']    = False
                ob['mitigated'] = True
