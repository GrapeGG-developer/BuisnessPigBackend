#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бизнес Поросята — игровой сервер (без внешних зависимостей).
Раздаёт статику (index.html), хранит онлайн-рейтинг и биржу ПК.

Запуск:
    python3 server.py          # http://localhost:8000

API:
    GET  /api/leaderboard      -> {"online": N, "players": [{id,name,score}, ...]}
    POST /api/leaderboard      -> body {id, name, score}

    GET  /api/pcmarket         -> {"online": N, "listings": [{id,sellerId,sellerName,name,power,price}, ...]}
    POST /api/pcmarket         -> body {sellerId, sellerName, name, power, price}
    POST /api/pcmarket/buy     -> body {buyerId, id}
    POST /api/pcmarket/cancel  -> body {sellerId, id}
    POST /api/pcmarket/claim   -> body {sellerId}  -> {"sales":[{name,power,price}, ...]}
"""
import json
import os
import threading
import time
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", 8000))
ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "leaderboard.json")
MK = os.path.join(ROOT, "market.json")
ONLINE_WINDOW = 300  # секунд активности, чтобы считаться "в сети"

_lock = threading.Lock()


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def pc_price_range(power):
    value = max(50, int(power * 250))
    return int(value * 0.5), int(value * 5)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/leaderboard":
            with _lock:
                db = load_json(DB)
            now = time.time()
            online = sum(1 for p in db.values() if now - p.get("ts", 0) < ONLINE_WINDOW)
            players = [
                {"id": k, "name": v.get("name", "Фермер"), "score": v.get("score", 0)}
                for k, v in db.items()
            ]
            players.sort(key=lambda p: -p["score"])
            self._json({"online": online, "players": players[:50]})
            return
        if path == "/api/pcmarket":
            with _lock:
                mk = load_json(MK)
            listings = mk.get("listings", [])
            now = time.time()
            online = sum(
                1 for l in listings if now - l.get("ts", 0) < ONLINE_WINDOW
            )
            self._json({"online": online, "listings": listings[:100]})
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/leaderboard":
            data = self._body()
            pid = str(data.get("id", ""))[:40]
            name = (str(data.get("name", "")) or "Фермер")[:20]
            try:
                score = max(0, int(data.get("score", 0)))
            except Exception:
                score = 0
            if not pid:
                self._json({"error": "no id"}, 400)
                return
            with _lock:
                db = load_json(DB)
                prev = db.get(pid, {})
                db[pid] = {
                    "name": name,
                    "score": max(score, prev.get("score", 0)),
                    "ts": time.time(),
                }
                save_json(DB, db)
            self._json({"ok": True})
            return

        if path == "/api/pcmarket":
            data = self._body()
            seller_id = str(data.get("sellerId", ""))[:40]
            seller_name = (str(data.get("sellerName", "")) or "Фермер")[:20]
            name = (str(data.get("name", "")) or "ПК")[:24]
            try:
                power = max(0, int(data.get("power", 0)))
                price = int(data.get("price", 0))
            except Exception:
                power, price = 0, 0
            if not seller_id:
                self._json({"error": "no sellerId"}, 400)
                return
            lo, hi = pc_price_range(power)
            if price < lo or price > hi:
                self._json({"error": "price out of range %d-%d" % (lo, hi)}, 400)
                return
            lid = "L" + uuid.uuid4().hex[:12]
            with _lock:
                mk = load_json(MK)
                mk.setdefault("listings", []).append({
                    "id": lid, "sellerId": seller_id, "sellerName": seller_name,
                    "name": name, "power": power, "price": price, "ts": time.time(),
                })
                save_json(MK, mk)
            self._json({"ok": True, "id": lid})
            return

        if path == "/api/pcmarket/buy":
            data = self._body()
            buyer = str(data.get("buyerId", ""))[:40]
            lid = str(data.get("id", ""))[:40]
            if not buyer or not lid:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                mk = load_json(MK)
                listings = mk.get("listings", [])
                item = next((l for l in listings if l["id"] == lid), None)
                if not item:
                    self._json({"error": "not found"}, 404)
                    return
                if item["sellerId"] == buyer:
                    self._json({"error": "own listing"}, 400)
                    return
                mk["listings"] = [l for l in listings if l["id"] != lid]
                mk.setdefault("sales", []).append({
                    "sellerId": item["sellerId"], "name": item["name"],
                    "power": item["power"], "price": item["price"], "ts": time.time(),
                })
                save_json(MK, mk)
            self._json({"ok": True, "name": item["name"], "power": item["power"]})
            return

        if path == "/api/pcmarket/cancel":
            data = self._body()
            seller = str(data.get("sellerId", ""))[:40]
            lid = str(data.get("id", ""))[:40]
            with _lock:
                mk = load_json(MK)
                before = len(mk.get("listings", []))
                mk["listings"] = [
                    l for l in mk.get("listings", [])
                    if not (l["id"] == lid and l["sellerId"] == seller)
                ]
                if len(mk["listings"]) == before:
                    self._json({"error": "not found"}, 404)
                    return
                save_json(MK, mk)
            self._json({"ok": True})
            return

        if path == "/api/pcmarket/claim":
            data = self._body()
            seller = str(data.get("sellerId", ""))[:40]
            with _lock:
                mk = load_json(MK)
                sales = [s for s in mk.get("sales", []) if s["sellerId"] == seller]
                mk["sales"] = [s for s in mk.get("sales", []) if s["sellerId"] != seller]
                save_json(MK, mk)
            self._json({"sales": [{"name": s["name"], "power": s["power"], "price": s["price"]} for s in sales]})
            return

        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    print("Бизнес Поросята · сервер запущен на http://0.0.0.0:%d" % PORT)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
