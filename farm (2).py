"""Огород: ферма в мини-аппе (грядки, семена, рост, продажа).
Подключение в main.py:  import farm;  await farm.init() в lifespan;  farm.mount(app, _api_user)
"""
import os
import random
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

import stats

HERE = os.path.dirname(os.path.abspath(__file__))
REFRESH = 1800                       # магазин обновляется каждые 30 минут
START_BEDS = 4                        # открыты сразу, только на 1 ферме
PER_FARM = 12                         # грядок на каждой ферме
FARMS = 3                             # всего овощных ферм
FARM_COSTS = {1: 10000, 2: 20000}     # цена прокачки: с 1 на 2 ферму, со 2 на 3 (после покупки всех грядок)
BED_BASE, BED_GROWTH = 500, 1.25      # цена 5-й грядки и рост цены с каждой следующей


def bed_price(n):
    """Цена n-й грядки; нумерация сквозная через все фермы (5 ... 36), каждая следующая дороже."""
    return int(round(BED_BASE * BED_GROWTH ** (n - START_BEDS - 1), -1))


def _beds(g):
    return [g["beds"], g["beds2"], g["beds3"]]
JOKER_CHANCE = 5                     # % найти Джокера при сборе Древа Джокера

# редкость: (значок, название, шанс появиться в магазине при обновлении)
RARITY = {
    "common": ("⚪", "Обычное", 1.0),
    "uncommon": ("🟢", "Необычное", 0.8),
    "rare": ("🔵", "Редкое", 0.5),
    "epic": ("🟣", "Эпическое", 0.3),
    "legendary": ("🟠", "Легендарное", 0.15),
    "mythic": ("🔴", "Мифическое", 0.05),
}
# id, значок, название, редкость, цена семени, рост (сек), цена продажи
_SEEDS = [
    ("carrot", "🥕", "Морковь", "common", 20, 60, 30),
    ("strawberry", "🍓", "Клубника", "common", 50, 180, 80),
    ("corn", "🌽", "Кукуруза", "common", 100, 360, 170),
    ("tomato", "🍅", "Помидор", "common", 200, 600, 350),
    ("potato", "🥔", "Картофель", "common", 350, 900, 620),
    ("cucumber", "🥒", "Огурец", "uncommon", 600, 1500, 1100),
    ("eggplant", "🍆", "Баклажан", "uncommon", 1000, 2400, 1900),
    ("pumpkin", "🎃", "Тыква", "uncommon", 1800, 3600, 3500),
    ("melon", "🍉", "Арбуз", "uncommon", 3000, 5400, 6000),
    ("chili", "🌶", "Чили", "uncommon", 5000, 7200, 10500),
    ("grape", "🍇", "Виноград", "rare", 9000, 10800, 19000),
    ("pineapple", "🍍", "Ананас", "rare", 15000, 14400, 33000),
    ("coconut", "🥥", "Кокос", "rare", 25000, 21600, 58000),
    ("mushroom", "🍄", "Волшебный гриб", "rare", 40000, 28800, 98000),
    ("sunflower", "🌻", "Золотой подсолнух", "epic", 70000, 43200, 180000),
    ("moonberry", "🫐", "Лунная ягода", "epic", 120000, 57600, 320000),
    ("rose", "🌹", "Роза Вегаса", "epic", 200000, 72000, 560000),
    ("crystal", "💎", "Кристальная лоза", "legendary", 400000, 86400, 1200000),
    ("fireflower", "🔥", "Огненный цветок", "legendary", 800000, 129600, 2600000),
    ("joker_tree", "🃏", "Древо Джокера", "mythic", 2000000, 172800, 7000000),
]
CATALOG = [dict(id=s[0], icon=s[1], name=s[2], rarity=s[3], price=s[4], grow=s[5], sell=s[6]) for s in _SEEDS]
BY_ID = {c["id"]: c for c in CATALOG}


class GardenError(Exception):
    pass


def shop_ids():
    """Ассортимент текущего получаса (одинаковый у всех игроков) и секунд до обновления."""
    now = time.time()
    rng = random.Random(int(now // REFRESH))
    ids = [c["id"] for c in CATALOG if c["rarity"] == "common" or rng.random() < RARITY[c["rarity"]][2]]
    return ids, REFRESH - int(now) % REFRESH


def _int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


async def init():
    p = stats.pool
    if not p:
        return
    await p.execute("CREATE TABLE IF NOT EXISTS garden (tg_id BIGINT PRIMARY KEY, beds INT NOT NULL DEFAULT 4)")
    await p.execute("CREATE TABLE IF NOT EXISTS garden_plots (tg_id BIGINT, slot INT, seed TEXT NOT NULL, "
                    "ready TIMESTAMPTZ NOT NULL, PRIMARY KEY (tg_id, slot))")
    await p.execute("ALTER TABLE garden ADD COLUMN IF NOT EXISTS level INT NOT NULL DEFAULT 1")   # level>=2: курятник куплен (см. coop.py)
    for col, d in (("beds2", 0), ("beds3", 0), ("farms", 1)):
        await p.execute(f"ALTER TABLE garden ADD COLUMN IF NOT EXISTS {col} INT NOT NULL DEFAULT {d}")
    await p.execute("CREATE TABLE IF NOT EXISTS garden_items (tg_id BIGINT, kind TEXT, item TEXT, "
                    "qty INT NOT NULL DEFAULT 0, PRIMARY KEY (tg_id, kind, item))")


_LOCK = ("INSERT INTO garden (tg_id) VALUES ($1) ON CONFLICT (tg_id) DO UPDATE SET tg_id=garden.tg_id "
         "RETURNING beds, beds2, beds3, farms, level")        # блокирует строку игрока: двойные нажатия не ломают счёт


async def _add(c, uid, kind, item, n):
    await c.execute("INSERT INTO garden_items (tg_id,kind,item,qty) VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT (tg_id,kind,item) DO UPDATE SET qty=garden_items.qty+$4", uid, kind, item, n)


async def _pay(c, uid, cost):
    left = await c.fetchval("UPDATE players SET coins=coins-$2 WHERE tg_id=$1 AND coins>=$2 RETURNING coins", uid, cost)
    if left is None:
        raise GardenError(f"Не хватает Козырей: нужно {cost} ♠️")


async def state(uid):
    p = stats.pool
    g = await p.fetchrow(_LOCK, uid)
    bl = _beds(g)
    owned = sum(bl)
    me = await p.fetchrow("SELECT coins, jokers FROM players WHERE tg_id=$1", uid)
    plots = await p.fetch("SELECT slot, seed, GREATEST(CEIL(EXTRACT(EPOCH FROM (ready - now()))),0)::int AS left "
                          "FROM garden_plots WHERE tg_id=$1 ORDER BY slot", uid)
    items = await p.fetch("SELECT kind, item, qty FROM garden_items WHERE tg_id=$1 AND qty>0", uid)
    ids, shop_in = shop_ids()
    return {
        "coins": me["coins"] if me else 0, "jokers": me["jokers"] if me else 0,
        "farms": g["farms"], "per_farm": PER_FARM, "beds": bl,
        "bed_cost": bed_price(owned + 1) if owned < FARMS * PER_FARM else 0,
        "farm_cost": FARM_COSTS.get(g["farms"], 0),
        "farm_ready": bl[g["farms"] - 1] >= PER_FARM,
        "plots": [{"slot": r["slot"], "seed": r["seed"], "left": r["left"]} for r in plots],
        "seeds": {r["item"]: r["qty"] for r in items if r["kind"] == "seed"},
        "crops": {r["item"]: r["qty"] for r in items if r["kind"] == "crop"},
        "shop": ids, "shop_in": shop_in,
        "catalog": CATALOG, "rarity": {k: list(v[:2]) for k, v in RARITY.items()},
    }


# ----------------------------------------------------------------- действия

async def buy(uid, b):
    sid, qty = b.get("seed"), _int(b.get("qty"), 1)
    if sid not in BY_ID or not 1 <= qty <= 99:
        raise GardenError("Неверный запрос")
    if sid not in shop_ids()[0]:
        raise GardenError("Этого семени уже нет в магазине")
    async with stats.pool.acquire() as c, c.transaction():
        await c.fetchval(_LOCK, uid)
        await _pay(c, uid, BY_ID[sid]["price"] * qty)
        await _add(c, uid, "seed", sid, qty)
    return {"msg": f"{BY_ID[sid]['icon']} {BY_ID[sid]['name']} ×{qty} куплено"}


async def plant(uid, b):
    sid, slot = b.get("seed"), _int(b.get("slot"), -1)
    if sid not in BY_ID:
        raise GardenError("Неверный запрос")
    async with stats.pool.acquire() as c, c.transaction():
        g = await c.fetchrow(_LOCK, uid)
        f, i = divmod(slot, PER_FARM)
        if not 0 <= slot < FARMS * PER_FARM or f >= g["farms"] or i >= _beds(g)[f]:
            raise GardenError("Эта грядка ещё закрыта")
        if await c.fetchval("SELECT 1 FROM garden_plots WHERE tg_id=$1 AND slot=$2", uid, slot):
            raise GardenError("Грядка занята")
        left = await c.fetchval("UPDATE garden_items SET qty=qty-1 WHERE tg_id=$1 AND kind='seed' AND item=$2 "
                                "AND qty>0 RETURNING qty", uid, sid)
        if left is None:
            raise GardenError("У вас нет такого семени")
        await c.execute("INSERT INTO garden_plots (tg_id,slot,seed,ready) VALUES ($1,$2,$3, now() + make_interval(secs => $4))",
                        uid, slot, sid, float(BY_ID[sid]["grow"]))
    return {}


async def harvest(uid, b):
    """slot задан: собрать одну грядку, иначе все созревшие."""
    one = b.get("slot") is not None
    async with stats.pool.acquire() as c, c.transaction():
        await c.fetchval(_LOCK, uid)
        sql = "DELETE FROM garden_plots WHERE tg_id=$1 AND ready<=now()" + (" AND slot=$2" if one else "") + " RETURNING seed"
        rows = await c.fetch(sql, uid, *([_int(b.get("slot"), -1)] if one else []))
        if not rows:
            raise GardenError("Пока ничего не созрело")
        counts = {}
        for r in rows:
            counts[r["seed"]] = counts.get(r["seed"], 0) + 1
        jokers = 0
        for sid, n in counts.items():
            await _add(c, uid, "crop", sid, n)
            if sid == "joker_tree":
                jokers += sum(1 for _ in range(n) if random.random() * 100 < JOKER_CHANCE)
        if jokers:
            await c.execute("UPDATE players SET jokers=jokers+$2 WHERE tg_id=$1", uid, jokers)
    return {"harvested": len(rows), "jokers": jokers}


async def sell(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await c.fetchval(_LOCK, uid)
        rows = await c.fetch("DELETE FROM garden_items WHERE tg_id=$1 AND kind='crop' RETURNING item, qty", uid)
        total = sum(BY_ID[r["item"]]["sell"] * r["qty"] for r in rows if r["item"] in BY_ID)
        if not total:
            raise GardenError("Нечего продавать")
        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, total)
    return {"sold": total}


async def bed(uid, b):
    """Купить следующую грядку выбранной (уже открытой) фермы."""
    f = _int(b.get("farm"), -1)
    async with stats.pool.acquire() as c, c.transaction():
        g = await c.fetchrow(_LOCK, uid)
        if not 0 <= f < g["farms"]:
            raise GardenError("Эта ферма ещё закрыта")
        bl = _beds(g)
        if bl[f] >= PER_FARM:
            raise GardenError("Все грядки этой фермы уже открыты")
        await _pay(c, uid, bed_price(sum(bl) + 1))
        col = ("beds", "beds2", "beds3")[f]
        await c.execute(f"UPDATE garden SET {col}={col}+1 WHERE tg_id=$1", uid)
    return {}


async def farmup(uid, b):
    """Прокачка: открыть следующую овощную ферму (нужны все грядки текущей)."""
    async with stats.pool.acquire() as c, c.transaction():
        g = await c.fetchrow(_LOCK, uid)
        if g["farms"] >= FARMS:
            raise GardenError("Все фермы уже открыты")
        if _beds(g)[g["farms"] - 1] < PER_FARM:
            raise GardenError(f"Сначала откройте все грядки фермы {g['farms']}")
        await _pay(c, uid, FARM_COSTS[g["farms"]])
        await c.execute("UPDATE garden SET farms=farms+1 WHERE tg_id=$1", uid)
    return {}


ACTIONS = {"buy": buy, "plant": plant, "harvest": harvest, "sell": sell, "bed": bed, "farmup": farmup}


def helpers(auth):
    """Общие помощники для API огорода и курятника. auth(request) -> пользователь Telegram или HTTPException 401."""

    async def uid_of(request):
        user = auth(request)
        if not stats.pool:
            raise HTTPException(status_code=503)
        uid = user["id"]
        await stats.ensure(uid, (user.get("first_name") or user.get("username") or str(uid))[:20])
        return uid

    async def run(request, fn, view):
        uid = await uid_of(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        try:
            extra = await fn(uid, body)
        except GardenError as e:
            return {"error": str(e), "state": await view(uid)}
        return {**extra, "state": await view(uid)}

    return uid_of, run


def mount(app, auth):
    uid_of, run = helpers(auth)

    @app.get("/farm")
    async def page():
        return FileResponse(os.path.join(HERE, "farm.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/api/garden")
    async def get_state(request: Request):
        return await state(await uid_of(request))

    @app.post("/api/garden/{act}")
    async def do(act: str, request: Request):
        if act not in ACTIONS:
            raise HTTPException(status_code=404)
        return await run(request, ACTIONS[act], state)
