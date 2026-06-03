"""
utils/chart_exporter.py
=======================
Saves a chart snapshot whenever a signal fires.
"""

import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from datetime import datetime
from utils.logger import get_logger

log = get_logger("chart")


def save_signal_chart(df, ob: dict, signal: dict, filename: str = None):
    """
    Save a chart showing the last 100 bars, the OB zone, and the signal.

    Parameters
    ----------
    df      : DataFrame with OHLCV + EMA_1H columns (full data)
    ob      : order block dict
    signal  : signal dict (entry, sl, tp1, tp2, type, bar_index, timestamp)
    filename: output path; auto-generated if None
    """
    from config.settings import CHART_DIR

    if filename is None:
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        sig = signal['type']
        filename = os.path.join(CHART_DIR, f"signal_{sig}_{ts}.png")

    WINDOW = 100
    end_i  = signal['bar_index'] + 10
    start_i = max(0, end_i - WINDOW)
    df_plot = df.iloc[start_i:end_i].copy().reset_index()
    n = len(df_plot)

    fig, ax = plt.subplots(figsize=(14, 7), facecolor='#0d0f14')
    ax.set_facecolor('#0d0f14')
    ax.tick_params(colors='#8a8f98', labelsize=7)
    for sp in ax.spines.values():
        sp.set_edgecolor('#1e2230')

    # Candles
    for i, row in df_plot.iterrows():
        color = '#00c853' if row['Close'] >= row['Open'] else '#d50000'
        ax.plot([i, i], [row['Low'], row['High']], color=color, linewidth=0.6, zorder=2)
        bottom = min(row['Open'], row['Close'])
        height = abs(row['Close'] - row['Open']) or 0.00005
        ax.add_patch(plt.Rectangle(
            (i - 0.3, bottom), 0.6, height, color=color, zorder=3))

    # EMA
    if 'EMA_1H' in df_plot.columns:
        ax.plot(range(n), df_plot['EMA_1H'].values,
                color='#ffd600', linewidth=0.9, linestyle='--', alpha=0.7)

    # OB zone
    ob_x = ob['bar_index'] - start_i
    ob_x = max(0, ob_x)
    color_zone = '#1b5e20' if ob['type'] == 'Bullish' else '#b71c1c'
    edge_color  = '#4caf50' if ob['type'] == 'Bullish' else '#f44336'
    ax.add_patch(plt.Rectangle(
        (ob_x, ob['bottom']), n - ob_x, ob['top'] - ob['bottom'],
        facecolor=color_zone, edgecolor=edge_color,
        linewidth=0.8, alpha=0.35, zorder=1))

    # Signal marker + SL/TP lines
    sig_x = signal['bar_index'] - start_i
    if 0 <= sig_x < n:
        if signal['type'] == 'BUY':
            ax.annotate('▲', (sig_x, signal['entry']),
                        color='#00e676', fontsize=14, ha='center', va='top', zorder=10)
        else:
            ax.annotate('▼', (sig_x, signal['entry']),
                        color='#ff5252', fontsize=14, ha='center', va='bottom', zorder=10)

        x0, x1 = sig_x / n, 1.0
        ax.axhline(signal['sl'],  xmin=x0, xmax=x1,
                   color='#f44336', linewidth=1.0, linestyle=':', alpha=0.9)
        ax.axhline(signal['tp1'], xmin=x0, xmax=x1,
                   color='#ffeb3b', linewidth=0.8, linestyle=':', alpha=0.7)
        ax.axhline(signal['tp2'], xmin=x0, xmax=x1,
                   color='#00e676', linewidth=1.0, linestyle=':', alpha=0.9)

        # Labels on right edge
        price_range = df_plot['High'].max() - df_plot['Low'].min()
        offset = price_range * 0.003
        for price, label, col in [
            (signal['sl'],  'SL',  '#f44336'),
            (signal['tp1'], 'TP1', '#ffeb3b'),
            (signal['tp2'], 'TP2', '#00e676'),
            (signal['entry'], 'ENTRY', '#ffffff'),
        ]:
            ax.text(n - 0.5, price + offset, label,
                    color=col, fontsize=8, va='bottom', ha='right')

    sym = signal.get('symbol', 'EURUSD')
    ax.set_title(
        f"{sym} 5m | {ob['type']} OB | {signal['type']} Signal | "
        f"{signal['timestamp'].strftime('%Y-%m-%d %H:%M') if hasattr(signal['timestamp'], 'strftime') else signal['timestamp']}",
        color='#e0e0e0', fontsize=11, fontweight='bold', pad=10)
    ax.set_xlim(0, n)
    ax.set_ylabel('Price', color='#8a8f98', fontsize=9)

    legend_elements = [
        mpatches.Patch(color=edge_color, alpha=0.6,
                       label=f"{ob['type']} OB zone"),
    ]
    ax.legend(handles=legend_elements, loc='upper left',
              framealpha=0.2, labelcolor='white', fontsize=8)

    plt.tight_layout(pad=1.5)
    plt.savefig(filename, dpi=130, bbox_inches='tight', facecolor='#0d0f14')
    plt.close()
    log.info(f"Chart saved → {filename}")
    return filename
