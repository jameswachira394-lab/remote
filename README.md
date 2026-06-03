# Institutional Order Block Trading System — MT5 Live
## Folder Structure
```
ob_mt5_system/
├── main.py                 # Entry point — run this
├── config/
│   └── settings.py         # All config (symbol, risk, MT5 creds)
├── core/
│   ├── data_feed.py        # MT5 OHLCV fetcher + indicators
│   ├── ob_detector.py      # Order Block detection engine
│   ├── signal_engine.py    # Retest + confirmation logic
│   └── risk_manager.py     # Position sizing, SL/TP calc
├── mt5/
│   ├── connector.py        # MT5 connect/disconnect/health
│   ├── order_executor.py   # Place/modify/close orders
│   └── position_manager.py # Monitor open trades, move to BE
├── utils/
│   ├── logger.py           # Structured rotating log
│   ├── notifier.py         # Optional Telegram alerts
│   └── chart_exporter.py   # Chart snapshots on signal
├── logs/                   # Auto-created log files
├── data/                   # Cached bar data (CSV)
└── charts/                 # Signal chart snapshots
```
## Quick Start
1. Install: `pip install MetaTrader5 pandas numpy matplotlib`
2. Edit `config/settings.py` — set your MT5 path, account, symbol
3. Run: `python main.py`
## Requirements
- MetaTrader5 terminal installed and logged in
- Python 3.8+ (Windows or Wine on Linux)
- pip packages: MetaTrader5, pandas, numpy, matplotlib
