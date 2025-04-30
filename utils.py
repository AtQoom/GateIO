
import time
import hmac
import hashlib
import base64
import requests
import json

class GateioClient:
    def __init__(self, api_key, secret_key, base_url, config):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.config = config
        self.headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'KEY': self.api_key,
        }

    def _generate_signature(self, method, path, query_string='', body=''):
        t = str(int(time.time()))
        hashed_payload = hashlib.sha512(body.encode()).hexdigest()
        signature_string = f"{method.upper()}\n{path}\n{query_string}\n{hashed_payload}\n{t}"
        sign = hmac.new(self.secret_key.encode(), signature_string.encode(), hashlib.sha512).hexdigest()
        return sign, t

    def _send_request(self, method, path, query=None, body=None):
        query_string = '&'.join(f"{k}={v}" for k, v in query.items()) if query else ''
        full_url = f"{self.base_url}{path}" + (f"?{query_string}" if query else '')
        body_str = json.dumps(body) if body else ''
        sign, timestamp = self._generate_signature(method, path, query_string, body_str)

        headers = self.headers.copy()
        headers.update({
            'Timestamp': timestamp,
            'SIGN': sign,
        })

        response = requests.request(method, full_url, headers=headers, data=body_str)
        return response.json()

    def get_position(self, symbol):
        return self._send_request("GET", f"/api/v4/futures/usdt/positions", {"contract": symbol})

    def get_open_orders(self, symbol):
        return self._send_request("GET", f"/api/v4/futures/usdt/orders", {"contract": symbol})

    def cancel_all_orders(self, symbol):
        return self._send_request("DELETE", f"/api/v4/futures/usdt/orders", {"contract": symbol})

    def get_account(self):
        return self._send_request("GET", "/api/v4/futures/usdt/accounts")

    def open_position(self, symbol, side, size):
        body = {
            "contract": symbol,
            "size": size if side == "long" else -size,
            "price": 0,
            "close": False,
            "reduce_only": False,
            "tif": "ioc"
        }
        return self._send_request("POST", "/api/v4/futures/usdt/orders", body=body)

    def close_position(self, symbol, size):
        body = {
            "contract": symbol,
            "size": -size,
            "price": 0,
            "close": True,
            "reduce_only": True,
            "tif": "ioc"
        }
        return self._send_request("POST", "/api/v4/futures/usdt/orders", body=body)

    def get_avg_price_and_size(self, symbol):
        pos = self.get_position(symbol)
        for p in pos:
            if p["contract"] == symbol and abs(float(p["size"])) > 0:
                return float(p["entry_price"]), float(p["size"])
        return 0, 0
