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
COINS = os.path.join(ROOT, "coins.json")
CLAN = os.path.join(ROOT, "clan.json")
RACE = os.path.join(ROOT, "race.json")
SYNC = os.path.join(ROOT, "sync.json")
FIAT = os.path.join(ROOT, "fiat.json")
BIZM = os.path.join(ROOT, "bizmarket.json")
SRV = os.path.join(ROOT, "servers.json")
ONLINE_WINDOW = 300  # секунд активности, чтобы считаться "в сети"

# ===== Базовые валюты (обмен) =====
# Курс двигается от сделок игроков: покупка поднимает цену, продажа опускает.
FX_SEED = [
    ("SWD", "Свиноллар", "fa-dollar-sign", "85bb65", 100.0),
    ("EURP", "Евросёнок", "fa-euro-sign", "6366f1", 120.0),
    ("GBP", "Свинофунт", "fa-sterling-sign", "ef4444", 140.0),
    ("JPY", "Свинойена", "fa-yen-sign", "f59e0b", 1.0),
    ("CNY", "Свиноюань", "fa-won-sign", "ec4899", 14.0),
    ("RUB", "Свинорубль", "fa-ruble-sign", "10b981", 1.2),
    ("INR", "Свинорупия", "fa-indian-rupee-sign", "38bdf8", 9.0),
    ("BTC", "Свинобиткоин", "fa-bitcoin-sign", "fbbf24", 50000.0),
]


def seed_fiat(data):
    cur = data.setdefault("fiat", {})
    for code, name, icon, color, price in FX_SEED:
        cur.setdefault(code, {
            "code": code, "name": name, "icon": icon, "color": color,
            "price": price, "volume": 0, "moved": 0,
        })
    return cur

# ===== Экономика монет игроков (bonding curve) =====
# price = COIN_SLOPE * supply. Покупка чеканит новые юниты (supply растёт, цена растёт),
# продажа сжигает юниты (supply падает, цена падает). Резерв — монеты в пуле ликвидности.
COIN_SLOPE = 0.1
COIN_INIT_SUPPLY = 1000            # стартовая эмиссия выдаётся эмитенту бесплатно
COIN_START_PRICE = int(COIN_SLOPE * COIN_INIT_SUPPLY)  # 100
ISSUER_FEE_PCT = 0.02              # комиссия эмитенту по умолчанию (2%)
TAX_PCT = 0.01                     # налог на сделку по умолчанию (1%)
COIN_MAX_TX = 100000               # максимум юнитов за одну сделку

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


def clamp_icon(s):
    # иконка: только буквы/цифры/дефис (безопасно для class-атрибута)
    import re as _re
    return _re.sub(r"[^A-Za-z0-9\-]", "", str(s or ""))[:30] or "fa-coins"


def clamp_pct(x, lo, hi, default):
    try:
        v = float(x)
    except Exception:
        return default
    return max(lo, min(hi, v))


def clamp_color(s):
    # цвет: hex-строка без решётки
    import re as _re
    c = _re.sub(r"[^0-9a-fA-F]", "", str(s or ""))[:6]
    return c or "eab308"


def curve_buy_cost(supply, n):
    # интеграл цены по кривой price(s)=COIN_SLOPE*s при покупке n юнитов
    return int(math.ceil(COIN_SLOPE * (supply * n + n * (n + 1) / 2.0)))


def curve_sell_refund(supply, n):
    # возврат при продаже (сжигании) n юнитов из текущего supply
    return int(math.floor(COIN_SLOPE * (supply * n - n * (n - 1) / 2.0)))


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
                    "clan": v.get("clan", ""),
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

        if path == "/api/coins":
            with _lock:
                cn = load_json(COINS)
            coins = cn.get("coins", {})
            out = [
                {"id": k,
                 "name": v.get("name", ""),
                 "sym": v.get("sym", ""),
                 "icon": v.get("icon", "fa-coins"),
                 "iconColor": v.get("iconColor", "eab308"),
                 "issuer": v.get("issuer", ""),
                 "issuerName": v.get("issuerName", ""),
                 "price": max(1, int(v.get("price", COIN_START_PRICE))),
                 "supply": int(v.get("supply", COIN_INIT_SUPPLY)),
                 "reserve": int(v.get("reserve", 0)),
                 "volume": int(v.get("volume", 0)),
                 "fees": int(v.get("fees", 0)),
                 "tax": int(v.get("tax", 0)),
                 "feePct": float(v.get("feePct", 2.0)),
                 "taxPct": float(v.get("taxPct", 1.0)),
                 "history": v.get("history", [])}
                for k, v in coins.items()
            ]
            out.sort(key=lambda c: -c["price"])
            self._json({"coins": out[:50]})
            return

        if path == "/api/fiat":
            with _lock:
                fn = load_json(FIAT)
                seed_fiat(fn)
                save_json(FIAT, fn)
            out = [dict(v) for v in fn.get("fiat", {}).values()]
            out.sort(key=lambda c: -c["price"])
            self._json({"fiat": out})
            return

        if path == "/api/servers":
            with _lock:
                sv = load_json(SRV)
            rows = []
            now = time.time()
            for k, v in sv.get("servers", {}).items():
                if now - v.get("ts", 0) > 3600:
                    continue
                rows.append({"id": k, "ownerId": v.get("ownerId", ""), "ownerName": v.get("ownerName", ""),
                             "name": v.get("name", ""), "power": v.get("power", 0), "conn": v.get("conn", 0)})
            rows.sort(key=lambda s: -s["conn"])
            self._json({"servers": rows})
            return

        if path == "/api/bizmarket":
            with _lock:
                bm = load_json(BIZM)
            now = time.time()
            rows = []
            for k, v in bm.get("listings", {}).items():
                if now - v.get("ts", 0) > 86400:
                    continue
                rows.append({"id": k, "sellerId": v.get("sellerId", ""), "sellerName": v.get("sellerName", ""),
                             "bizId": v.get("bizId", ""), "bizName": v.get("bizName", ""),
                             "prod": v.get("prod", 0), "price": v.get("price", 0)})
            self._json({"online": len(rows), "listings": rows})
            return

        if path == "/api/social/hacks":
            qs = urlparse(self.path).query
            to_id = ""
            for part in qs.split("&"):
                if part.startswith("toId="):
                    from urllib.parse import unquote
                    to_id = unquote(part[5:])[:40]
            if not to_id:
                self._json({"hacks": []})
                return
            with _lock:
                sc = load_json(SOC)
            hacks = sc.get("hacks", {}).get(to_id, [])
            self._json({"hacks": hacks[-20:]})
            return

        if path == "/api/race":
            with _lock:
                rc = load_json(RACE)
                today = time.strftime("%Y-%m-%d")
                if rc.get("date") != today:
                    rc = {"date": today, "players": {}}
            players = rc.get("players", {})
            rows = [{"id": k, "name": v.get("name", "Фермер"), "amount": v.get("amount", 0)} for k, v in players.items()]
            rows.sort(key=lambda p: -p["amount"])
            self._json({"date": today, "players": rows[:20]})
            return

        if path == "/api/social/clans":
            with _lock:
                cl = load_json(CLAN)
            clans = cl.get("clans", {})
            out = [
                {"name": v.get("name", ""), "members": len(v.get("members", {}))}
                for v in clans.values()
            ]
            out.sort(key=lambda c: -c["members"])
            self._json({"clans": out[:30]})
            return

        if path == "/api/sync":
            qs = urlparse(self.path).query
            key = ""
            for part in qs.split("&"):
                if part.startswith("key="):
                    from urllib.parse import unquote
                    key = unquote(part[4:])
            key = key[:120]
            if not key:
                self._json({"error": "no key"}, 400)
                return
            with _lock:
                sync = load_json(SYNC)
            entry = sync.get("saves", {}).get(key)
            if not entry:
                self._json({"error": "not found"}, 404)
                return
            self._json({"ts": entry.get("ts", 0), "data": entry.get("data", {})})
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
            try:
                parts = list(data.get("parts", []))[:40]
            except Exception:
                parts = []
            try:
                soft = dict(data.get("soft", {}))
                soft = {str(k)[:24]: v for k, v in list(soft.items())[:40]}
            except Exception:
                soft = {}
            lid = "L" + uuid.uuid4().hex[:12]
            with _lock:
                mk = load_json(MK)
                mk.setdefault("listings", []).append({
                    "id": lid, "sellerId": seller_id, "sellerName": seller_name,
                    "name": name, "power": power, "price": price, "ts": time.time(),
                    "parts": parts, "soft": soft,
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
            self._json({"ok": True, "name": item["name"], "power": item["power"],
                        "parts": item.get("parts", []), "soft": item.get("soft", {})})
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
                    "clan": clamp_str(data.get("clan", ""), 16),
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
            currency = clamp_str(data.get("currency", "") or "coins", 40)
            try:
                amount = int(data.get("amount", 0))
            except Exception:
                amount = 0
            label = ""
            if currency not in ("coins", "acorns", "snouts", "candies"):
                # допускаем перевод монет игроков (валюта = id монеты)
                with _lock:
                    cn = load_json(COINS)
                coin = cn.get("coins", {}).get(currency)
                if not coin:
                    self._json({"error": "bad currency"}, 400)
                    return
                label = coin.get("sym", "COIN")
            else:
                label = {"coins": "Монеты", "acorns": "Жёлуди", "snouts": "Пятачки", "candies": "Конфеты"}[currency]
            if not from_id or not to_id or from_id == to_id:
                self._json({"error": "bad request"}, 400)
                return
            if amount <= 0 or amount > 10 ** 18:
                self._json({"error": "bad amount"}, 400)
                return
            with _lock:
                soc = load_json(SOC)
                gifts = soc.setdefault("gifts", {})
                gifts.setdefault(to_id, []).append({
                    "fromId": from_id, "fromName": from_name,
                    "currency": currency, "amount": amount, "label": label, "ts": time.time(),
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
            out = []
            for g in gifts:
                out.append({
                    "fromId": g.get("fromId", ""),
                    "fromName": g.get("fromName", "Фермер"),
                    "currency": g.get("currency", "coins"),
                    "amount": g.get("amount", 0),
                    "label": g.get("label", ""),
                })
            self._json({"gifts": out})
            return

        # ----- монеты игроков: выпуск, торговля по bonding curve, чеканка, комиссия -----
        if path == "/api/coin/issue":
            data = self._body()
            issuer = clamp_str(data.get("id", ""), 40)
            name = clamp_str(data.get("name", ""), 14)
            sym = clamp_str(data.get("sym", "").upper(), 6)
            icon = clamp_icon(data.get("icon", "fa-coins"))
            icon_color = clamp_color(data.get("iconColor", "eab308"))
            issuer_name = clamp_str(data.get("issuerName", "") or "Фермер", 20)
            try:
                supply = int(data.get("supply", COIN_INIT_SUPPLY))
            except Exception:
                supply = COIN_INIT_SUPPLY
            supply = max(100, min(100000, supply))
            fee_pct = clamp_pct(data.get("feePct", 2.0), 0.0, 10.0, 2.0)
            tax_pct = clamp_pct(data.get("taxPct", 1.0), 0.0, 5.0, 1.0)
            start_price = max(1, int(COIN_SLOPE * supply))
            if not issuer or not name or not sym:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                cn = load_json(COINS)
                coins = cn.setdefault("coins", {})
                # один игрок — одна монета
                for c in coins.values():
                    if c.get("issuer") == issuer:
                        self._json({"error": "already issued"}, 400)
                        return
                cid = "C" + uuid.uuid4().hex[:10]
                coins[cid] = {
                    "name": name, "sym": sym, "icon": icon, "iconColor": icon_color,
                    "issuer": issuer, "issuerName": issuer_name,
                    "supply": supply, "reserve": 0,
                    "feePct": fee_pct, "taxPct": tax_pct,
                    "price": start_price, "history": [start_price],
                    "fees": 0, "tax": 0, "volume": 0, "created": time.time(),
                }
                save_json(COINS, cn)
            self._json({"ok": True, "id": cid, "supply": supply, "price": start_price,
                        "icon": icon, "iconColor": icon_color, "feePct": fee_pct, "taxPct": tax_pct})
            return

        if path == "/api/coin/trade":
            data = self._body()
            cid = clamp_str(data.get("coinId", ""), 40)
            action = str(data.get("action", ""))
            try:
                n = max(1, int(data.get("n", 1)))
            except Exception:
                n = 1
            n = min(n, COIN_MAX_TX)
            if not cid or action not in ("buy", "sell", "mint"):
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                cn = load_json(COINS)
                coin = cn.get("coins", {}).get(cid)
                if not coin:
                    self._json({"error": "not found"}, 404)
                    return
                supply = int(coin.get("supply", COIN_INIT_SUPPLY))
                reserve = int(coin.get("reserve", 0))
                fee_frac = float(coin.get("feePct", 2.0)) / 100.0
                tax_frac = float(coin.get("taxPct", 1.0)) / 100.0

                def _apply(volume):
                    fee = int(volume * fee_frac) if fee_frac > 0 else 0
                    tax = int(volume * tax_frac) if tax_frac > 0 else 0
                    coin["fees"] = coin.get("fees", 0) + fee
                    coin["tax"] = coin.get("tax", 0) + tax
                    return fee, tax

                if action == "buy" or action == "mint":
                    curve = curve_buy_cost(supply, n)
                    fee, tax = _apply(curve)
                    supply += n
                    reserve += curve
                    coin["volume"] = coin.get("volume", 0) + curve + fee + tax
                    cost = curve + fee + tax
                    coin["supply"] = supply
                    coin["reserve"] = reserve
                    price = max(1, int(COIN_SLOPE * supply))
                    coin["price"] = price
                    hist = coin.setdefault("history", [])
                    hist.append(price)
                    if len(hist) > 60:
                        hist.pop(0)
                    save_json(COINS, cn)
                    self._json({"price": price, "cost": cost, "curve": curve, "fee": fee,
                                "tax": tax, "supply": supply, "reserve": reserve})
                    return
                # sell
                if supply <= 1:
                    self._json({"error": "no liquidity"}, 400)
                    return
                n = min(n, supply - 1)
                curve = curve_sell_refund(supply, n)
                refund_base = min(curve, reserve)
                fee, tax = _apply(refund_base)
                net = max(0, refund_base - fee - tax)
                supply -= n
                reserve = max(0, reserve - refund_base)
                coin["volume"] = coin.get("volume", 0) + refund_base
                coin["supply"] = supply
                coin["reserve"] = reserve
                price = max(1, int(COIN_SLOPE * supply))
                coin["price"] = price
                hist = coin.setdefault("history", [])
                hist.append(price)
                if len(hist) > 60:
                    hist.pop(0)
                save_json(COINS, cn)
            self._json({"price": price, "refund": net, "fee": fee, "tax": tax,
                        "supply": supply, "reserve": reserve})
            return

        if path == "/api/coin/claim":
            data = self._body()
            issuer = clamp_str(data.get("id", ""), 40)
            with _lock:
                cn = load_json(COINS)
                total = 0
                for c in cn.get("coins", {}).values():
                    if c.get("issuer") == issuer:
                        total += c.get("fees", 0)
                        c["fees"] = 0
                save_json(COINS, cn)
            self._json({"amount": total})
            return

        # ----- обмен валют (форекс) -----
        if path == "/api/fiat/trade":
            data = self._body()
            code = clamp_str(str(data.get("code", "")).upper(), 8)
            action = str(data.get("action", ""))
            try:
                n = max(1, int(data.get("n", 1)))
            except Exception:
                n = 1
            n = min(n, 1000000)
            if action not in ("buy", "sell"):
                self._json({"error": "bad action"}, 400)
                return
            with _lock:
                fn = load_json(FIAT)
                seed_fiat(fn)
                f = fn["fiat"].get(code)
                if not f:
                    self._json({"error": "not found"}, 404)
                    return
                price = float(f.get("price", 100.0))
                if action == "buy":
                    cost = int(n * price) + 1
                    f["price"] = round(price * (1.0 + 0.00002 * n), 6)
                    f["volume"] = int(f.get("volume", 0)) + n
                    f["moved"] = int(f.get("moved", 0)) + n
                    save_json(FIAT, fn)
                    self._json({"ok": True, "cost": cost, "price": f["price"], "n": n})
                    return
                refund = max(0, int(n * price))
                f["price"] = round(max(0.01, price * (1.0 - 0.00002 * n)), 6)
                f["volume"] = max(0, int(f.get("volume", 0)) - n)
                f["moved"] = int(f.get("moved", 0)) + n
                save_json(FIAT, fn)
            self._json({"ok": True, "refund": refund, "price": f["price"], "n": n})
            return

        # ----- бизнес-биржа (продажа бизнесов) -----
        if path == "/api/bizmarket":
            data = self._body()
            seller = clamp_str(data.get("sellerId", ""), 40)
            biz_id = clamp_str(data.get("bizId", ""), 40)
            try:
                price = max(1, int(data.get("price", 0)))
            except Exception:
                price = 1
            if not seller or not biz_id:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                bm = load_json(BIZM)
                listings = bm.setdefault("listings", {})
                lid = "B" + uuid.uuid4().hex[:10]
                listings[lid] = {
                    "sellerId": seller,
                    "sellerName": clamp_str(data.get("sellerName", "") or "Фермер", 20),
                    "bizId": biz_id,
                    "bizName": clamp_str(data.get("bizName", ""), 24),
                    "prod": int(data.get("prod", 0)),
                    "price": price,
                    "ts": time.time(),
                }
                save_json(BIZM, bm)
            self._json({"ok": True, "id": lid})
            return

        if path == "/api/bizmarket/buy":
            data = self._body()
            buyer = clamp_str(data.get("buyerId", ""), 40)
            lid = clamp_str(data.get("id", ""), 40)
            with _lock:
                bm = load_json(BIZM)
                listing = bm.get("listings", {}).get(lid)
                if not listing:
                    self._json({"error": "not found"}, 404)
                    return
                if listing.get("sellerId") == buyer:
                    self._json({"error": "own listing"}, 400)
                    return
                del bm["listings"][lid]
                sales = bm.setdefault("sales", {})
                sales.setdefault(listing["sellerId"], []).append({
                    "bizId": listing["bizId"], "bizName": listing["bizName"],
                    "price": listing["price"], "ts": time.time(),
                })
                save_json(BIZM, bm)
            self._json({"ok": True, "bizId": listing["bizId"], "bizName": listing["bizName"],
                        "prod": listing["prod"], "price": listing["price"]})
            return

        if path == "/api/bizmarket/cancel":
            data = self._body()
            seller = clamp_str(data.get("sellerId", ""), 40)
            lid = clamp_str(data.get("id", ""), 40)
            with _lock:
                bm = load_json(BIZM)
                listing = bm.get("listings", {}).get(lid)
                if listing and listing.get("sellerId") == seller:
                    del bm["listings"][lid]
                    save_json(BIZM, bm)
            self._json({"ok": True})
            return

        if path == "/api/bizmarket/claim":
            data = self._body()
            seller = clamp_str(data.get("sellerId", ""), 40)
            with _lock:
                bm = load_json(BIZM)
                sales = bm.get("sales", {}).pop(seller, [])
                save_json(BIZM, bm)
            total = sum(s.get("price", 0) for s in sales)
            self._json({"amount": total, "sales": sales})
            return

        # ----- серверы игроков -----
        if path == "/api/servers":
            data = self._body()
            owner = clamp_str(data.get("ownerId", ""), 40)
            if not owner:
                self._json({"error": "no id"}, 400)
                return
            with _lock:
                sv = load_json(SRV)
                servers = sv.setdefault("servers", {})
                sid = "S" + uuid.uuid4().hex[:10]
                servers[sid] = {
                    "ownerId": owner,
                    "ownerName": clamp_str(data.get("ownerName", "") or "Фермер", 20),
                    "name": clamp_str(data.get("name", "") or "Сервер", 24),
                    "power": int(data.get("power", 0)),
                    "conn": 0,
                    "ts": time.time(),
                }
                save_json(SRV, sv)
            self._json({"ok": True, "id": sid})
            return

        if path == "/api/servers/connect":
            data = self._body()
            sid = clamp_str(data.get("serverId", ""), 40)
            pid = clamp_str(data.get("playerId", ""), 40)
            if not sid or not pid:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                sv = load_json(SRV)
                srv = sv.get("servers", {}).get(sid)
                if not srv:
                    self._json({"error": "not found"}, 404)
                    return
                conns = srv.setdefault("conns", {})
                if pid in conns:
                    self._json({"error": "already connected"}, 400)
                    return
                conns[pid] = time.time()
                srv["conn"] = len(conns)
                save_json(SRV, sv)
            self._json({"ok": True, "conn": srv["conn"]})
            return

        # ----- взломы -----
        if path == "/api/hack":
            data = self._body()
            frm = clamp_str(data.get("fromId", ""), 40)
            to = clamp_str(data.get("toId", ""), 40)
            if not frm or not to or frm == to:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                sc = load_json(SOC)
                hacks = sc.setdefault("hacks", {})
                hacks.setdefault(to, []).append({
                    "fromName": clamp_str(data.get("fromName", "") or "Хакер", 20),
                    "reward": int(data.get("reward", 0)),
                    "ts": time.time(),
                })
                sc["hacks"] = {k: v[-30:] for k, v in hacks.items()}
                save_json(SOC, sc)
            self._json({"ok": True})
            return

        # ----- гонка дня -----
        if path == "/api/race":
            data = self._body()
            pid = clamp_str(data.get("id", ""), 40)
            name = clamp_str(data.get("name", "") or "Фермер", 20)
            try:
                amount = max(0, int(data.get("amount", 0)))
            except Exception:
                amount = 0
            if not pid:
                self._json({"error": "no id"}, 400)
                return
            with _lock:
                rc = load_json(RACE)
                today = time.strftime("%Y-%m-%d")
                if rc.get("date") != today:
                    rc = {"date": today, "players": {}}
                players = rc.setdefault("players", {})
                prev = players.get(pid, {})
                players[pid] = {"name": name, "amount": max(amount, prev.get("amount", 0))}
                save_json(RACE, rc)
            self._json({"ok": True})
            return

        # ----- кланы -----
        if path == "/api/social/clan/create":
            data = self._body()
            pid = clamp_str(data.get("id", ""), 40)
            name = clamp_str(data.get("name", ""), 16)
            if not pid or not name:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                cl = load_json(CLAN)
                clans = cl.setdefault("clans", {})
                key = name.lower()
                if key in clans:
                    self._json({"error": "exists"}, 400)
                    return
                clans[key] = {"name": name, "members": {pid: True}, "createdBy": pid}
                save_json(CLAN, cl)
            self._json({"ok": True})
            return

        if path == "/api/social/clan/join":
            data = self._body()
            pid = clamp_str(data.get("id", ""), 40)
            name = clamp_str(data.get("name", ""), 16)
            if not pid or not name:
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                cl = load_json(CLAN)
                clan = cl.get("clans", {}).get(name.lower())
                if not clan:
                    self._json({"error": "not found"}, 404)
                    return
                clan.setdefault("members", {})[pid] = True
                save_json(CLAN, cl)
            self._json({"ok": True})
            return

        if path == "/api/sync":
            data = self._body()
            key = clamp_str(data.get("key", ""), 120)
            try:
                ts = int(data.get("ts", 0))
            except Exception:
                ts = 0
            save_data = data.get("data")
            if not key or not isinstance(save_data, dict):
                self._json({"error": "bad request"}, 400)
                return
            with _lock:
                sync = load_json(SYNC)
                saves = sync.setdefault("saves", {})
                prev = saves.get(key, {})
                if ts >= prev.get("ts", 0):
                    saves[key] = {"ts": ts, "data": save_data}
                    save_json(SYNC, sync)
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    print("Бизнес Поросята · сервер запущен на http://0.0.0.0:%d" % PORT)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
