import MetaTrader5 as mt5

print("MetaTrader5 package version:", mt5.__version__)

path = r"C:\Program Files\MetaTrader 5\terminal64.exe"
print(f"Trying to initialize with path: {path}")

if not mt5.initialize(path=path):
    print("initialize() failed, error code =", mt5.last_error())
else:
    print("initialize() successful!")
    print(mt5.terminal_info())
    mt5.shutdown()
