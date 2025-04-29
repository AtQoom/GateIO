position = {
    "symbol": None,
    "side": None,
    "entry_price": None,
    "entry_time": None,
    "size": None
}

def store_position(symbol, side, price, entry_price):
    position.update({
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "entry_time": time.time(),
    })

# size 필드 삭제됨
position = {
    "symbol": None,
    "side": None,
    "entry_price": None,
    "entry_time": None,
}

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
