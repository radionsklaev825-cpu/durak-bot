"""Мясокомбинат: отдельный мини-апп (животные растут, их нужно кормить и поить, потом забой: 3 куска мяса).
Открывается после полной прокачки курятника (coop.py) и покупки за MEAT_COST.
Подключение в main.py:  import meat;  await meat.init() в lifespan;  meat.mount(app, _api_user)
"""
import os
import random
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

import stats
from coop import PAGES, PER_PAGE, SHOP_REFRESH, SHOP_SIZE, SHOP_W, START_CELLS
from farm import HERE, RARITY, GardenError, _LOCK, _add, _int, _pay, helpers

MEAT_COST = 100000                     # цена открытия мясокомбината (после полной прокачки курятника)
CELL_BASE, CELL_GROWTH = 8000, 1.28    # цена 3-й ячейки и рост цены с каждой следующей (сквозь все страницы)
PAGE_COSTS = {1: 60000, 2: 120000}     # прокачка: со страницы 1 на 2 и со 2 на 3
FEED_OK = 0.8                          # кормить и поить можно, когда шкала ниже 80%
PIECES = 3                             # кусков мяса с одного животного
# id, значок, название, редкость, цена, рост (сек, считается только пока сыто и напоено), цена куска мяса,
# на сколько секунд хватает полной шкалы сытости и жажды, цена одной кормёжки
_ANIMALS = [
    ("rabbit", "🐇", "Кролик", "common", 2000, 900, 1200, 600, 120),
    ("goat", "🐐", "Коза", "common", 5000, 1200, 3000, 720, 300),
    ("sheep", "🐑", "Овца", "common", 9000, 1500, 5400, 900, 540),
    ("pig", "🐖", "Свинья", "common", 15000, 1800, 9000, 900, 900),
    ("cow", "🐄", "Корова", "common", 25000, 2400, 15000, 1200, 1500),
    ("boar", "🐗", "Кабан", "uncommon", 45000, 3000, 27000, 1500, 2700),
    ("deer", "🦌", "Олень", "uncommon", 80000, 3600, 48000, 1800, 4800),
    ("ox", "🐂", "Бык", "uncommon", 130000, 4200, 78000, 1800, 7800),
    ("llama", "🦙", "Лама", "uncommon", 200000, 4800, 120000, 2100, 12000),
    ("horse", "🐎", "Лошадь", "uncommon", 300000, 5400, 180000, 2400, 18000),
    ("buffalo", "🐃", "Буйвол", "rare", 450000, 6000, 270000, 2700, 27000),
    ("bison", "🦬", "Бизон", "rare", 700000, 6600, 420000, 3000, 42000),
    ("camel", "🐪", "Верблюд", "rare", 1100000, 7200, 660000, 3000, 66000),
    ("kangaroo", "🦘", "Кенгуру", "rare", 1700000, 7800, 1020000, 3300, 102000),
    ("rhino", "🦏", "Носорог", "epic", 2600000, 8400, 1560000, 3600, 156000),
    ("hippo", "🦛", "Бегемот", "epic", 4000000, 9000, 2400000, 3600, 240000),
    ("croc", "🐊", "Крокодил", "epic", 6500000, 9600, 3900000, 3900, 390000),
    ("elephant", "🐘", "Слон", "legendary", 10000000, 10800, 6000000, 4200, 600000),
    ("unicorn", "🦄", "Единорог", "legendary", 18000000, 12600, 10800000, 4800, 1080000),
    ("dragon", "🐉", "Дракон", "mythic", 40000000, 14400, 24000000, 5400, 2400000),
]
ANIMALS = [dict(id=a[0], icon=a[1], name=a[2], rarity=a[3], price=a[4], grow=a[5], meat=a[6], care=a[7], food=a[8])
           for a in _ANIMALS]
ANI = {a["id"]: a for a in ANIMALS}


def cell_price(k):
    """Цена k-й ячейки мясокомбината (k = 3 ... 27, нумерация сквозная через все страницы)."""
    return int(round(CELL_BASE * CELL_GROWTH ** (k - START_CELLS - 1), -2))


def shop_animals():
    """5 животных текущего получаса (одинаковые у всех игроков; редкость слотов меняется) и секунд до обновления."""
    now = time.time()
    rng = random.Random(f"meat{int(now // SHOP_REFRESH)}")
    pool, out = ANIMALS[:], []
    for _ in range(SHOP_SIZE):
        rars = [r for r in SHOP_W if any(a["rarity"] == r for a in pool)]
        r = rng.choices(rars, [SHOP_W[x] for x in rars])[0]
        a = rng.choice([x for x in pool if x["rarity"] == r])
        pool.remove(a)
        out.append(a["id"])
    return out, SHOP_REFRESH - int(now) % SHOP_REFRESH


def _open_slots(cells):
    return [p * PER_PAGE + i for p in range(PAGES) for i in range(cells[p])]


def _cells(g):
    return [g["mp1"], g["mp2"], g["mp3"]]


def _coop_done(g):
    """Курятник прокачан полностью: все 3 страницы и все ячейки на 3-й."""
    return g["cp_pages"] >= PAGES and g["cp3"] >= PER_PAGE


def _open(g):
    return _coop_done(g) and g["mp_open"] >= 1


_GQ = "SELECT cp_pages, cp3, mp_open, mp_pages, mp1, mp2, mp3 FROM garden WHERE tg_id=$1"


async def init():
    p = stats.pool
    if not p:
        return
    for col, d in (("mp_open", 0), ("mp_pages", 1), ("mp1", START_CELLS), ("mp2", 0), ("mp3", 0)):
        await p.execute(f"ALTER TABLE garden ADD COLUMN IF NOT EXISTS {col} INT NOT NULL DEFAULT {d}")
    await p.execute("CREATE TABLE IF NOT EXISTS meat_animals (tg_id BIGINT, slot INT, animal TEXT NOT NULL, "
                    "grown DOUBLE PRECISION NOT NULL DEFAULT 0, upd TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "fed TIMESTAMPTZ NOT NULL DEFAULT now(), wat TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "PRIMARY KEY (tg_id, slot))")


_AQ = ("SELECT slot, animal, grown, EXTRACT(EPOCH FROM (now()-upd))::float AS age, "
       "EXTRACT(EPOCH FROM (fed-now()))::float AS fed_left, EXTRACT(EPOCH FROM (wat-now()))::float AS wat_left "
       "FROM meat_animals WHERE tg_id=$1")


def _settled(r):
    """Сколько секунд животное уже выросло: рост идёт, только пока обе шкалы не пусты."""
    a = ANI[r["animal"]]
    active = max(0.0, r["age"] + min(0.0, r["fed_left"], r["wat_left"]))
    return min(float(a["grow"]), r["grown"] + active)


async def _need_meat(c, uid):
    await c.fetchval(_LOCK, uid)
    g = await c.fetchrow(_GQ, uid)
    if not _coop_done(g):
        raise GardenError("Мясокомбинат заблокирован: прокачайте курятник до конца")
    if g["mp_open"] < 1:
        raise GardenError(f"Сначала откройте мясокомбинат за {MEAT_COST} ♠️")
    return g


async def _plant_state(uid, g):
    p = stats.pool
    rows = await p.fetch(_AQ + " ORDER BY slot", uid)
    items = await p.fetch("SELECT item, qty FROM garden_items WHERE tg_id=$1 AND kind='meat' AND qty>0", uid)
    cells = _cells(g)
    owned = sum(cells)
    shop, shop_in = shop_animals()
    return {
        "pages": g["mp_pages"], "per_page": PER_PAGE, "cells": cells,
        "cell_cost": cell_price(owned + 1) if owned < PAGES * PER_PAGE else 0,
        "page_cost": PAGE_COSTS.get(g["mp_pages"], 0), "page_ready": cells[g["mp_pages"] - 1] >= PER_PAGE,
        "shop": shop, "shop_in": shop_in, "pieces": PIECES, "feed_ok": FEED_OK,
        "animals": [{"slot": r["slot"], "animal": r["animal"], "grown": _settled(r),
                     "fed_left": max(0.0, r["fed_left"]), "wat_left": max(0.0, r["wat_left"])} for r in rows],
        "meat": {r["item"]: r["qty"] for r in items}, "cat": ANIMALS,
    }


async def meat_view(uid):
    """Состояние мини-аппа: закрыт (с прогрессом курятника), можно открыть или открыт (с животными)."""
    p = stats.pool
    await p.fetchval(_LOCK, uid)
    g = await p.fetchrow(_GQ, uid)
    me = await p.fetchrow("SELECT coins, jokers FROM players WHERE tg_id=$1", uid)
    out = {
        "unlocked": _open(g), "coop_done": _coop_done(g), "meat_cost": MEAT_COST,
        "coop_pages": g["cp_pages"], "coop_cells": 0, "coop_total": PAGES * PER_PAGE,
        "coins": me["coins"] if me else 0, "jokers": me["jokers"] if me else 0,
        "rarity": {k: list(v[:2]) for k, v in RARITY.items()},
    }
    cp = await p.fetchrow("SELECT cp1, cp2, cp3 FROM garden WHERE tg_id=$1", uid)
    out["coop_cells"] = cp["cp1"] + cp["cp2"] + cp["cp3"]
    if out["unlocked"]:
        out["plant"] = await _plant_state(uid, g)
    return out


# ----------------------------------------------------------------- действия

async def unlock(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await c.fetchval(_LOCK, uid)
        g = await c.fetchrow(_GQ, uid)
        if not _coop_done(g):
            raise GardenError("Сначала прокачайте курятник до конца")
        if g["mp_open"] >= 1:
            raise GardenError("Мясокомбинат уже открыт")
        await _pay(c, uid, MEAT_COST)
        await c.execute("UPDATE garden SET mp_open=1 WHERE tg_id=$1", uid)
    return {}


async def buycell(uid, b):
    page = _int(b.get("page"), -1)
    async with stats.pool.acquire() as c, c.transaction():
        g = await _need_meat(c, uid)
        cells = _cells(g)
        if not 0 <= page < g["mp_pages"]:
            raise GardenError("Эта страница ещё закрыта")
        if cells[page] >= PER_PAGE:
            raise GardenError("Все ячейки этой страницы уже открыты")
        await _pay(c, uid, cell_price(sum(cells) + 1))
        col = ("mp1", "mp2", "mp3")[page]
        await c.execute(f"UPDATE garden SET {col}={col}+1 WHERE tg_id=$1", uid)
    return {}


async def pageup(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        g = await _need_meat(c, uid)
        cells = _cells(g)
        if g["mp_pages"] >= PAGES:
            raise GardenError("Все страницы уже открыты")
        if cells[g["mp_pages"] - 1] < PER_PAGE:
            raise GardenError(f"Сначала откройте все ячейки страницы {g['mp_pages']}")
        await _pay(c, uid, PAGE_COSTS[g["mp_pages"]])
        await c.execute("UPDATE garden SET mp_pages=mp_pages+1 WHERE tg_id=$1", uid)
    return {}


async def buyanimal(uid, b):
    a = ANI.get(b.get("animal"))
    if not a:
        raise GardenError("Неверный запрос")
    if a["id"] not in shop_animals()[0]:
        raise GardenError("Этого животного уже нет в магазине")
    async with stats.pool.acquire() as c, c.transaction():
        g = await _need_meat(c, uid)
        used = {r["slot"] for r in await c.fetch("SELECT slot FROM meat_animals WHERE tg_id=$1", uid)}
        slot = next((s for s in _open_slots(_cells(g)) if s not in used), None)
        if slot is None:
            raise GardenError("Нет свободных ячеек: откройте новую или забейте животное")
        await _pay(c, uid, a["price"])
        await c.execute("INSERT INTO meat_animals (tg_id, slot, animal, grown, upd, fed, wat) VALUES "
                        "($1,$2,$3,0, now(), now()+make_interval(secs=>$4), now()+make_interval(secs=>$4))",
                        uid, slot, a["id"], float(a["care"]))
    return {}


async def _care(uid, b, kind):
    one = b.get("slot") is not None
    async with stats.pool.acquire() as c, c.transaction():
        await _need_meat(c, uid)
        rows = await c.fetch(_AQ + (" AND slot=$2" if one else "") + " FOR UPDATE",
                             uid, *([_int(b.get("slot"), -1)] if one else []))
        todo, total = [], 0
        for r in rows:
            a = ANI[r["animal"]]
            if (r["fed_left"] if kind == "feed" else r["wat_left"]) < a["care"] * FEED_OK:
                todo.append((r, a))
                total += a["food"] if kind == "feed" else 0
        if not todo:
            raise GardenError("Животное сыто" if kind == "feed" and one else "Животное напоено" if one
                              else "Сейчас никому не нужно" + (" корма" if kind == "feed" else " воды"))
        if total:
            await _pay(c, uid, total)
        col = "fed" if kind == "feed" else "wat"
        for r, a in todo:
            await c.execute(f"UPDATE meat_animals SET grown=$3, upd=now(), {col}=now()+make_interval(secs=>$4) "
                            "WHERE tg_id=$1 AND slot=$2", uid, r["slot"], _settled(r), float(a["care"]))
    return {"n": len(todo), "cost": total}


async def feed(uid, b):
    return await _care(uid, b, "feed")


async def water(uid, b):
    return await _care(uid, b, "water")


async def slaughter(uid, b):
    one = b.get("slot") is not None
    async with stats.pool.acquire() as c, c.transaction():
        await _need_meat(c, uid)
        rows = await c.fetch(_AQ + (" AND slot=$2" if one else "") + " FOR UPDATE",
                             uid, *([_int(b.get("slot"), -1)] if one else []))
        done = [r for r in rows if _settled(r) >= ANI[r["animal"]]["grow"] - 0.5]
        if not done:
            raise GardenError("Пока никто не вырос")
        for r in done:
            await c.execute("DELETE FROM meat_animals WHERE tg_id=$1 AND slot=$2", uid, r["slot"])
            await _add(c, uid, "meat", r["animal"], PIECES)
    return {"n": len(done), "pieces": len(done) * PIECES}


async def sellanimal(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await _need_meat(c, uid)
        aid = await c.fetchval("DELETE FROM meat_animals WHERE tg_id=$1 AND slot=$2 RETURNING animal", uid, _int(b.get("slot"), -1))
        if aid is None:
            raise GardenError("Тут нет животного")
        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, ANI[aid]["price"] // 2)
    return {}


async def sellmeat(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await _need_meat(c, uid)
        rows = await c.fetch("DELETE FROM garden_items WHERE tg_id=$1 AND kind='meat' RETURNING item, qty", uid)
        total = sum(ANI[r["item"]]["meat"] * r["qty"] for r in rows if r["item"] in ANI)
        if not total:
            raise GardenError("Нет мяса на продажу")
        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, total)
    return {"sold": total}


ACTIONS = {
    "unlock": unlock, "buycell": buycell, "pageup": pageup, "buyanimal": buyanimal, "feed": feed, "water": water,
    "slaughter": slaughter, "sellanimal": sellanimal, "sellmeat": sellmeat,
}


def mount(app, auth):
    uid_of, run = helpers(auth)

    @app.get("/meat")
    async def meat_page():
        return FileResponse(os.path.join(HERE, "meat.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/api/meat")
    async def meat_get(request: Request):
        return await meat_view(await uid_of(request))

    @app.post("/api/meat/{act}")
    async def meat_do(act: str, request: Request):
        if act not in ACTIONS:
            raise HTTPException(status_code=404)
        return await run(request, ACTIONS[act], meat_view)
