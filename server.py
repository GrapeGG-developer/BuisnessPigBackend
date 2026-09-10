#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бизнес Поросята — игровой сервер (без внешних зависимостей).
Раздаёт статику (index.html), хранит онлайн-рейтинг, биржу ПК и соц-раздел.

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

    GET  /api/social/profiles  -> {"online": N, "players": [{id,name,code,level,prestige,power,skin,hat,coinName,coinSym,score,clicks,playTime,likes,online}, ...]}
    POST /api/social/profile   -> body {id, name, code, level, prestige, power, skin, hat, coinName, coinSym, score, clicks, playTime}
    POST /api/social/like      -> body {fromId, toId}   -> {"likes": N}
    POST /api/social/gift      -> body {fromId, fromName, toId, amount} -> {"ok": true}
    POST /api/social/gifts/claim -> body {toId}  -> {"gifts": [{fromName, amount}, ...]}
"""
import json
import math
import os
import random
import threading
import time
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", 8000))
ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "leaderboard.json")
MK = os.path.join(ROOT, "market.json")
SOC = os.path.join(ROOT, "social.json")
ST = os.path.join(ROOT, "stock.json")
ONLINE_WINDOW = 300  # секунд активности, чтобы считаться "в сети"

# Базовые активы фондовой биржи (цены общие для всех игроков).
STOCK_BASE = {
    "acrn": {"price": 10},
    "trfl": {"price": 150},
    "mud": {"price": 1000},
}
STOCK_TICK = 8  # сек между "шагами" цены на сервере

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


def clamp_str(s, n):
    return (str(s) if s is not None else "")[:n]


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

    # ---------- GET ----------
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
            online = sum(1 for l in listings if now - l.get("ts", 0) < ONLINE_WINDOW)
            self._json({"online": online, "listings": listings[:100]})
            return

        if path == "/api/social/profiles":
            with _lock:
                soc = load_json(SOC)
            profiles = soc.get("profiles", {})
            now = time.time()
            players = []
            for pid, v in profiles.items():
                players.append({
                    "id": pid,
                    "name": v.get("name", "Фермер"),
                    "code": v.get("code", ""),
                    "level": v.get("level", 1),
                    "prestige": v.get("prestige", 0),
                    "power": v.get("power", 0),
                    "skin": v.get("skin", "classic"),
                    "hat": v.get("hat", "none"),
                    "coinName": v.get("coinName", ""),
                    "coinSym": v.get("coinSym", ""),
                    "score": v.get("score", 0),
                    "clicks": v.get("clicks", 0),
                    "playTime": v.get("playTime", 0),
                    "likes": v.get("likes", 0),
                    "online": (now - v.get("ts", 0)) < ONLINE_WINDOW,
                })
            players.sort(key=lambda p: -p["score"])
            online = sum(1 for p in players if p["online"])
            self._json({"online": online, "players": players[:50]})
            return

        if path == "/api/stock":
            with _lock:
                st = load_json(ST)
                assets = st.get("assets", {})
                # убедимся, что все активы существуют (даже после частичных торгов)
                for k in STOCK_BASE:
                    assets.setdefault(k, {"price": STOCK_BASE[k]["price"], "history": [STOCK_BASE[k]["price"]]})
                now = time.time()
                if now - st.get("ts", 0) >= STOCK_TICK:
                    for k in STOCK_BASE:
                        a = assets[k]
                        drift = random.uniform(-0.03, 0.03)
                        price = max(1, round(a["price"] * (1 + drift)))
                        a["price"] = price
                        hist = a.setdefault("history", [])
                        hist.append(price)
                        if len(hist) > 40:
                            hist.pop(0)
                    st["ts"] = now
                    save_json(ST, st)
            out = [{"id": k, "price": assets[k]["price"], "history": assets[k].get("history", [])} for k in STOCK_BASE]
            self._json({"assets": out})
            return

        super().do_GET()

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/leaderboard":
            data = self._body()
            pid = clamp_str(data.get("id", ""), 40)
            name = clamp_str(data.get("name", "") or "Фермер", 20)
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
            seller_id = clamp_str(data.get("sellerId", ""), 40)
            seller_name = clamp_str(data.get("sellerName", "") or "Фермер", 20)
            name = clamp_str(data.get("name", "") or "ПК", 24)
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
            buyer = clamp_str(data.get("buyerId", ""), 40)
            lid = clamp_str(data.get("id", ""), 40)
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
            seller = clamp_str(data.get("sellerId", ""), 40)
            lid = clamp_str(data.get("id", ""), 40)
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
            seller = clamp_str(data.get("sellerId", ""), 40)
            with _lock:
                mk = load_json(MK)
                sales = [s for s in mk.get("sales", []) if s["sellerId"] == seller]
                mk["sales"] = [s for s in mk.get("sales", []) if s["sellerId"] != seller]
                save_json(MK, mk)
            self._json({"sales": [{"name": s["name"], "power": s["power"], "price": s["price"]} for s in sales]})
            return

        # ----- social -----
        if path == "/api/social/profile":
            data = self._body()
            pid = clamp_str(data.get("id", ""), 40)
            if not pid:
                self._json({"error": "no id"}, 400)
                return
            try:
                score = max(0, int(data.get("score", 0)))
                level = max(1, int(data.get("level", 1)))
                prestige = max(0, int(data.get("prestige", 0)))
                power = max(0, int(data.get("power", 0)))
                clicks = max(0, int(data.get("clicks", 0)))
                play_time = max(0, int(data.get("playTime", 0)))
            except Exception:
                score = level = prestige = power = clicks = play_time = 0
            with _lock:
                soc = load_json(SOC)
                profiles = soc.setdefault("profiles", {})
                prev = profiles.get(pid, {})
                profiles[pid] = {
                    "name": clamp_str(data.get("name", "") or "Фермер", 20),
                    "code": clamp_str(data.get("code", ""), 8),
                    "level": level,
                    "prestige": prestige,
                    "power": power,
                    "skin": clamp_str(data.get("skin", "classic"), 20),
                    "hat": clamp_str(data.get("hat", "none"), 20),
                    "coinName": clamp_str(data.get("coinName", ""), 14),
                    "coinSym": clamp_str(data.get("coinSym", ""), 6),
                    "score": max(score, prev.get("score", 0)),
                    "clicks": clicks,
                    "playTime": play_time,
                    "likes": prev.get("likes", 0),
                    "ts": time.time(),
                }
                save_json(SOC, soc)
            self._json({"ok": True})
            return

        if path == "/api/social/like":
            data = self._body()
            from_id = clamp_str(data.get("fromId", ""), 40)
            to_id = clamp_str(data.get("toId", ""), 40)
            if not from_id or not to_id or from_id == to_id:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                soc = load_json(SOC)
                profiles = soc.setdefault("profiles", {})
                prof = profiles.get(to_id)
                if not prof:
                    self._json({"error": "not found"}, 404)
                    return
                prof["likes"] = prof.get("likes", 0) + 1
                save_json(SOC, soc)
            self._json({"likes": profiles[to_id]["likes"]})
            return

        if path == "/api/social/gift":
            data = self._body()
            from_id = clamp_str(data.get("fromId", ""), 40)
            from_name = clamp_str(data.get("fromName", "") or "Фермер", 20)
            to_id = clamp_str(data.get("toId", ""), 40)
            try:
                amount = int(data.get("amount", 0))
            except Exception:
                amount = 0
            if not from_id or not to_id or from_id == to_id:
                self._json({"error": "bad request"}, 400)
                return
            if amount <= 0 or amount > 10 ** 15:
                self._json({"error": "bad amount"}, 400)
                return
            with _lock:
                soc = load_json(SOC)
                gifts = soc.setdefault("gifts", {})
                gifts.setdefault(to_id, []).append({
                    "fromName": from_name, "amount": amount, "ts": time.time(),
                })
                save_json(SOC, soc)
            self._json({"ok": True})
            return

        if path == "/api/social/gifts/claim":
            data = self._body()
            to_id = clamp_str(data.get("toId", ""), 40)
            with _lock:
                soc = load_json(SOC)
                gifts = soc.get("gifts", {}).pop(to_id, [])
                save_json(SOC, soc)
            self._json({"gifts": [{"fromName": g["fromName"], "amount": g["amount"]} for g in gifts]})
            return

        if path == "/api/stock/trade":
            data = self._body()
            aid = clamp_str(data.get("id", ""), 20)
            action = str(data.get("action", ""))
            try:
                n = max(1, int(data.get("n", 1)))
            except Exception:
                n = 1
            if aid not in STOCK_BASE or action not in ("buy", "sell"):
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                st = load_json(ST)
                assets = st.setdefault("assets", {})
                a = assets.setdefault(aid, {"price": STOCK_BASE[aid]["price"], "history": [STOCK_BASE[aid]["price"]]})
                if action == "buy":
                    a["price"] = max(1, int(math.ceil(a["price"] * (1 + 0.015 * min(n, 30)))))
                else:
                    a["price"] = max(1, int(math.floor(a["price"] * (1 - 0.012 * min(n, 30)))))
                hist = a.setdefault("history", [])
                hist.append(a["price"])
                if len(hist) > 40:
                    hist.pop(0)
                save_json(ST, st)
            self._json({"price": a["price"]})
            return

        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    print("Бизнес Поросята · сервер запущен на http://0.0.0.0:%d" % PORT)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
