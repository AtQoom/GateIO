import time

position = {
    "symbol": None,
    "side": None,
    "entry_price": None,
    "entry_time": None
}

def store_position(symbol, side, price):
    position.update({
        "symbol": symbol,
        "side": side,
        "entry_price": price,
        "entry_time": time.time()
    })

def clear_position():
    position.update({
        "symbol": None,
        "side": None,
        "entry_price": None,
        "entry_time": None
    })

def get_position():
    return position