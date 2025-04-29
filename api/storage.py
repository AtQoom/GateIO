position = {
    "symbol": None,
    "side": None,
    "entry_price": None,
    "entry_time": None,
    "size": None
}

def store_position(symbol, side, price, size):
    position.update({
        "symbol": symbol,
        "side": side,
        "entry_price": price,
        "entry_time": time.time(),
        "size": size
    })

def clear_position():
    position.update({
        "symbol": None,
        "side": None,
        "entry_price": None,
        "entry_time": None,
        "size": None
    })

def get_position():
    return position
