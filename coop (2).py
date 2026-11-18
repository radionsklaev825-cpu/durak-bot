"""Курятник: отдельный мини-апп (птицы, яйца, оплодотворённые яйца, инкубатор).
Открывается после полной прокачки овощной фермы (farm.py) и покупки за COOP_COST.
Подключение в main.py:  import coop;  await coop.init() в lifespan;  coop.mount(app, _api_user)
"""
import os
import random
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

import stats
from farm import FARMS, HERE, PER_FARM, RARITY, GardenError, _LOCK, _add, _beds, _int, _pay, helpers

# ---- курятник: открывается после полной прокачки 3 овощной фермы (все 3 фермы и все 36 грядок)
COOP_COST = 50000       # цена открытия курятника (после полной прокачки фермы)
PER_PAGE = 9            # ячеек на странице курятника
PAGES = 3               # страниц (как у овощных ферм, листаются)
START_CELLS = 2         # открыты сразу на 1 странице, остальные покупаются
CELL_BASE, CELL_GROWTH = 5000, 1.28    # цена 3-й ячейки и рост цены с каждой следующей (сквозь все страницы)
PAGE_COSTS = {1: 30000, 2: 60000}      # прокачка: со страницы 1 на 2 и со 2 на 3 (нужны все ячейки страницы)
SHOP_REFRESH, SHOP_SIZE = 1800, 5      # магазин птиц: 5 птиц, обновляется каждые 30 минут
SHOP_W = {"common": 45, "uncommon": 28, "rare": 16, "epic": 7, "legendary": 3, "mythic": 1}   # шансы редкостей в слотах магазина
EGG_CAP = 10            # сколько яиц копит одна птица
INCUBATORS = 2
HATCH_S = 900           # вылупление, сек
FERT_CHANCE = 0.10      # шанс, что яйцо оплодотворено
QUALITY = ["Обычное", "Хорошее", "Отличное", "Идеальное"]
Q_WEIGHTS = [70, 20, 8, 2]
BASE_W = {"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 1, "mythic": 0.2}   # шанс редкости при вылуплении
# id, значок, название, редкость, цена, раз в N сек яйцо, цена яйца (20 птиц; id первых восьми прежние)
_BIRDS = [
    ("chicken", "🐔", "Курица", "common", 3000, 600, 400),
    ("duck", "🦆", "Утка", "common", 6000, 720, 750),
    ("sparrow", "🐦", "Воробей", "common", 10000, 900, 1200),
    ("dove", "🕊", "Голубь", "common", 16000, 900, 1800),
    ("quail", "🐤", "Перепёлка", "common", 25000, 1200, 3400),
    ("goose", "🪿", "Гусь", "uncommon", 45000, 1500, 7500),
    ("turkey", "🦃", "Индюк", "uncommon", 80000, 1800, 14000),
    ("penguin", "🐧", "Пингвин", "uncommon", 130000, 2100, 24000),
    ("parrot", "🦜", "Попугай", "uncommon", 200000, 2400, 40000),
    ("swan", "🦢", "Лебедь", "uncommon", 300000, 2700, 62000),
    ("owl", "🦉", "Сова", "rare", 450000, 3000, 100000),
    ("eagle", "🦅", "Орёл", "rare", 700000, 3300, 165000),
    ("flamingo", "🦩", "Фламинго", "rare", 1100000, 3600, 280000),
    ("dodo", "🦤", "Дронт", "rare", 1700000, 3900, 450000),
    ("peacock", "🦚", "Павлин", "epic", 2600000, 4200, 750000),
    ("goldrooster", "🐓", "Золотой петух", "epic", 4000000, 4800, 1300000),
    ("blackswan", "🦢", "Чёрный лебедь", "epic", 6500000, 5400, 2300000),
    ("phoenix", "🔥", "Феникс", "legendary", 10000000, 6000, 4200000),
    ("thunderbird", "⚡", "Громовая птица", "legendary", 18000000, 7200, 8000000),
    ("simurgh", "🌈", "Радужный Симург", "mythic", 40000000, 10800, 24000000),
]
BIRDS = [dict(id=b[0], icon=b[1], name=b[2], rarity=b[3], price=b[4], every=b[5], egg=b[6]) for b in _BIRDS]
BIRD = {b["id"]: b for b in BIRDS}
_RIDX = {r: i for i, r in enumerate(RARITY)}
_NR = {r: sum(1 for b in BIRDS if b["rarity"] == r) for r in RARITY}


def cell_price(k):
    """Цена k-й ячейки курятника (k = 3 ... 27, нумерация сквозная через все страницы)."""
    return int(round(CELL_BASE * CELL_GROWTH ** (k - START_CELLS - 1), -2))


def shop_birds():
    """5 птиц текущего получаса (одинаковые у всех игроков; редкость слотов меняется) и секунд до обновления."""
    now = time.time()
    rng = random.Random(f"coop{int(now // SHOP_REFRESH)}")
    pool, out = BIRDS[:], []
    for _ in range(SHOP_SIZE):
        rars = [r for r in SHOP_W if any(b["rarity"] == r for b in pool)]
        r = rng.choices(rars, [SHOP_W[x] for x in rars])[0]
        b = rng.choice([x for x in pool if x["rarity"] == r])
        pool.remove(b)
        out.append(b["id"])
    return out, SHOP_REFRESH - int(now) % SHOP_REFRESH


def _open_slots(cells):
    return [p * PER_PAGE + i for p in range(PAGES) for i in range(cells[p])]


async def init():
    p = stats.pool
    if not p:
        return
    await p.execute("CREATE TABLE IF NOT EXISTS coop_birds (tg_id BIGINT, slot INT, bird TEXT NOT NULL, "
                    "last TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (tg_id, slot))")
    for col, d in (("cp_pages", 1), ("cp1", START_CELLS), ("cp2", 0), ("cp3", 0)):
        await p.execute(f"ALTER TABLE garden ADD COLUMN IF NOT EXISTS {col} INT NOT NULL DEFAULT {d}")
    await p.execute("CREATE TABLE IF NOT EXISTS coop_incubator (tg_id BIGINT, slot INT, quality INT NOT NULL, "
                    "ready TIMESTAMPTZ NOT NULL, PRIMARY KEY (tg_id, slot))")
    await p.execute("UPDATE garden g SET cp1 = GREATEST(g.cp1, LEAST(9, COALESCE((SELECT MAX(slot)+1 FROM coop_birds b "
                    "WHERE b.tg_id=g.tg_id AND b.slot<9), 0)))")      # птицы из старой версии не остаются в закрытых ячейках


async def _coop_state(uid):
    p = stats.pool
    birds = await p.fetch("SELECT slot, bird, EXTRACT(EPOCH FROM (now()-last))::float AS age FROM coop_birds "
                          "WHERE tg_id=$1 ORDER BY slot", uid)
    inc = await p.fetch("SELECT slot, quality, GREATEST(CEIL(EXTRACT(EPOCH FROM (ready-now()))),0)::int AS left "
                        "FROM coop_incubator WHERE tg_id=$1 ORDER BY slot", uid)
    items = await p.fetch("SELECT kind, item, qty FROM garden_items WHERE tg_id=$1 AND kind IN ('egg','fegg') AND qty>0", uid)
    fegg = []
    for r in items:
        if r["kind"] == "fegg":
            b, q = r["item"].split(":")
            fegg.append({"bird": b, "q": int(q), "qty": r["qty"]})
    g = await p.fetchrow("SELECT cp_pages, cp1, cp2, cp3 FROM garden WHERE tg_id=$1", uid)
    cells = [g["cp1"], g["cp2"], g["cp3"]]
    owned = sum(cells)
    shop, shop_in = shop_birds()
    return {
        "pages": g["cp_pages"], "per_page": PER_PAGE, "cells": cells,
        "cell_cost": cell_price(owned + 1) if owned < PAGES * PER_PAGE else 0,
        "page_cost": PAGE_COSTS.get(g["cp_pages"], 0), "page_ready": cells[g["cp_pages"] - 1] >= PER_PAGE,
        "shop": shop, "shop_in": shop_in, "egg_cap": EGG_CAP, "incs": INCUBATORS, "hatch_s": HATCH_S, "quality": QUALITY,
        "birds": [{"slot": r["slot"], "bird": r["bird"], "age": r["age"]} for r in birds],
        "inc": [{"slot": r["slot"], "q": r["quality"], "left": r["left"]} for r in inc],
        "eggs": {r["item"]: r["qty"] for r in items if r["kind"] == "egg"},
        "fegg": fegg, "birds_cat": BIRDS,
    }


# ----------------------------------------------------------------- курятник

def _farm_done(g):
    """Овощная ферма прокачана полностью: все 3 фермы и все грядки на 3-й."""
    return g["farms"] >= FARMS and g["beds3"] >= PER_FARM


def _coop_open(g):
    """level >= 2 в таблице garden = курятник куплен."""
    return _farm_done(g) and g["level"] >= 2


async def _need_coop(c, uid):
    g = await c.fetchrow(_LOCK, uid)
    if not _farm_done(g):
        raise GardenError("Курятник заблокирован: прокачайте овощную ферму до конца")
    if g["level"] < 2:
        raise GardenError(f"Сначала откройте курятник за {COOP_COST} ♠️")


async def unlock(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        g = await c.fetchrow(_LOCK, uid)
        if not _farm_done(g):
            raise GardenError("Сначала прокачайте овощную ферму до конца")
        if g["level"] >= 2:
            raise GardenError("Курятник уже открыт")
        await _pay(c, uid, COOP_COST)
        await c.execute("UPDATE garden SET level=2 WHERE tg_id=$1", uid)
    return {}


async def _free_slot(c, uid, table, cap, err):
    used = {r["slot"] for r in await c.fetch(f"SELECT slot FROM {table} WHERE tg_id=$1", uid)}
    for i in range(cap):
        if i not in used:
            return i
    raise GardenError(err)


async def _free_cell(c, uid, err):
    r = await c.fetchrow("SELECT cp1, cp2, cp3 FROM garden WHERE tg_id=$1", uid)
    used = {x["slot"] for x in await c.fetch("SELECT slot FROM coop_birds WHERE tg_id=$1", uid)}
    for s in _open_slots([r["cp1"], r["cp2"], r["cp3"]]):
        if s not in used:
            return s
    raise GardenError(err)


async def buycell(uid, b):
    page = _int(b.get("page"), -1)
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        r = await c.fetchrow("SELECT cp_pages, cp1, cp2, cp3 FROM garden WHERE tg_id=$1", uid)
        cells = [r["cp1"], r["cp2"], r["cp3"]]
        if not 0 <= page < r["cp_pages"]:
            raise GardenError("Эта страница ещё закрыта")
        if cells[page] >= PER_PAGE:
            raise GardenError("Все ячейки этой страницы уже открыты")
        await _pay(c, uid, cell_price(sum(cells) + 1))
        col = ("cp1", "cp2", "cp3")[page]
        await c.execute(f"UPDATE garden SET {col}={col}+1 WHERE tg_id=$1", uid)
    return {}


async def pageup(uid, b):
    """Прокачка курятника: открыть следующую страницу (нужны все ячейки текущей)."""
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        r = await c.fetchrow("SELECT cp_pages, cp1, cp2, cp3 FROM garden WHERE tg_id=$1", uid)
        cells = [r["cp1"], r["cp2"], r["cp3"]]
        if r["cp_pages"] >= PAGES:
            raise GardenError("Все страницы уже открыты")
        if cells[r["cp_pages"] - 1] < PER_PAGE:
            raise GardenError(f"Сначала откройте все ячейки страницы {r['cp_pages']}")
        await _pay(c, uid, PAGE_COSTS[r["cp_pages"]])
        await c.execute("UPDATE garden SET cp_pages=cp_pages+1 WHERE tg_id=$1", uid)
    return {}


async def buybird(uid, b):
    bd = BIRD.get(b.get("bird"))
    if not bd:
        raise GardenError("Неверный запрос")
    if bd["id"] not in shop_birds()[0]:
        raise GardenError("Этой птицы уже нет в магазине")
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        slot = await _free_cell(c, uid, "Нет свободных ячеек: откройте новую или продайте птицу")
        await _pay(c, uid, bd["price"])
        await c.execute("INSERT INTO coop_birds (tg_id, slot, bird) VALUES ($1,$2,$3)", uid, slot, bd["id"])
    return {}


async def sellbird(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        bid = await c.fetchval("DELETE FROM coop_birds WHERE tg_id=$1 AND slot=$2 RETURNING bird", uid, _int(b.get("slot"), -1))
        if bid is None:
            raise GardenError("Тут нет птицы")
        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, BIRD[bid]["price"] // 2)
    return {}


async def collect(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        rows = await c.fetch("SELECT slot, bird, EXTRACT(EPOCH FROM (now()-last))::float AS age FROM coop_birds "
                             "WHERE tg_id=$1 FOR UPDATE", uid)
        eggs = fert = 0
        for r in rows:
            every = BIRD[r["bird"]]["every"]
            n = min(EGG_CAP, int(r["age"] // every))
            if n <= 0:
                continue
            rest = 0.0 if n >= EGG_CAP else r["age"] - n * every
            await c.execute("UPDATE coop_birds SET last = now() - make_interval(secs => $3) WHERE tg_id=$1 AND slot=$2",
                            uid, r["slot"], rest)
            for _ in range(n):
                eggs += 1
                if random.random() < FERT_CHANCE:      # оплодотворённое яйцо: случайное качество
                    q = random.choices([1, 2, 3, 4], Q_WEIGHTS)[0]
                    await _add(c, uid, "fegg", f"{r['bird']}:{q}", 1)
                    fert += 1
                else:
                    await _add(c, uid, "egg", r["bird"], 1)
        if not eggs:
            raise GardenError("Яиц пока нет")
    return {"eggs": eggs, "fert": fert}


async def sell_eggs(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        rows = await c.fetch("DELETE FROM garden_items WHERE tg_id=$1 AND kind='egg' RETURNING item, qty", uid)
        total = sum(BIRD[r["item"]]["egg"] * r["qty"] for r in rows if r["item"] in BIRD)
        if not total:
            raise GardenError("Нет яиц на продажу")
        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, total)
    return {"sold": total}


def _fkey(b):
    bid, q = b.get("bird"), _int(b.get("q"))
    if bid not in BIRD or not 1 <= q <= 4:
        raise GardenError("Неверный запрос")
    return bid, q, f"{bid}:{q}"


async def sellfegg(uid, b):
    """Оплодотворённое яйцо продаётся вдвое дороже обычного."""
    bid, q, key = _fkey(b)
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        n = await c.fetchval("DELETE FROM garden_items WHERE tg_id=$1 AND kind='fegg' AND item=$2 RETURNING qty", uid, key)
        if not n:
            raise GardenError("Нет такого яйца")
        total = BIRD[bid]["egg"] * 2 * n
        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, total)
    return {"sold": total}


async def incubate(uid, b):
    bid, q, key = _fkey(b)
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        slot = await _free_slot(c, uid, "coop_incubator", INCUBATORS, "Все инкубаторы заняты")
        left = await c.fetchval("UPDATE garden_items SET qty=qty-1 WHERE tg_id=$1 AND kind='fegg' AND item=$2 "
                                "AND qty>0 RETURNING qty", uid, key)
        if left is None:
            raise GardenError("Нет такого яйца")
        await c.execute("INSERT INTO coop_incubator (tg_id, slot, quality, ready) VALUES ($1,$2,$3, now() + make_interval(secs => $4))",
                        uid, slot, q, float(HATCH_S))
    return {}


async def hatch(uid, b):
    """Вылупляется случайная птица; чем выше качество яйца, тем выше шанс редкой."""
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        slot = await _free_cell(c, uid, "Нет свободной ячейки: откройте новую или продайте птицу")
        q = await c.fetchval("DELETE FROM coop_incubator WHERE tg_id=$1 AND slot=$2 AND ready<=now() RETURNING quality",
                             uid, _int(b.get("slot"), -1))
        if q is None:
            raise GardenError("Ещё не вылупилось")
        k = 1 + 2 * (q - 1)
        bird = random.choices(BIRDS, [BASE_W[x["rarity"]] / _NR[x["rarity"]] * (k if _RIDX[x["rarity"]] >= 2 else 1) for x in BIRDS])[0]
        await c.execute("INSERT INTO coop_birds (tg_id, slot, bird) VALUES ($1,$2,$3)", uid, slot, bird["id"])
    return {"bird": bird["id"]}


COOP_ACTIONS = {
    "unlock": unlock, "buycell": buycell, "pageup": pageup, "buybird": buybird, "sellbird": sellbird, "collect": collect, "sell_eggs": sell_eggs,
    "sellfegg": sellfegg, "incubate": incubate, "hatch": hatch,
}


async def coop_view(uid):
    """Состояние мини-аппа курятника: закрыт (с прогрессом прокачки) или открыт (с птицами и яйцами)."""
    pool = stats.pool
    g = await pool.fetchrow(_LOCK, uid)
    me = await pool.fetchrow("SELECT coins, jokers FROM players WHERE tg_id=$1", uid)
    out = {
        "unlocked": _coop_open(g), "farm_done": _farm_done(g), "coop_cost": COOP_COST, "farms": g["farms"], "total_farms": FARMS, "beds": _beds(g), "per_farm": PER_FARM,
        "coins": me["coins"] if me else 0, "jokers": me["jokers"] if me else 0,
        "rarity": {k: list(v[:2]) for k, v in RARITY.items()},
    }
    if out["unlocked"]:
        out["coop"] = await _coop_state(uid)
    return out


def mount(app, auth):
    uid_of, run = helpers(auth)

    @app.get("/coop")
    async def coop_page():
        return FileResponse(os.path.join(HERE, "coop.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/api/coop")
    async def coop_get(request: Request):
        return await coop_view(await uid_of(request))

    @app.post("/api/coop/{act}")
    async def coop_do(act: str, request: Request):
        if act not in COOP_ACTIONS:
            raise HTTPException(status_code=404)
        return await run(request, COOP_ACTIONS[act], coop_view)
