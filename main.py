"""
main.py
=======
Institutional Order Block Live Trading System (v2)
==================================================
Entry point. Run this file to start the unified bot.

Usage:
    python main.py              # Live trading / headless engine
    python main.py --dry-run    # Signal detection only, no orders placed
    python main.py --close-all  # Emergency: close all open positions and exit
"""

import sys
import os

# Ensure project root is on path so imports work correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from v2.scheduler import main

if __name__ == "__main__":
    main()
