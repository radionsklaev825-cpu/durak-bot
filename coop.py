"""Курятник: отдельный мини-апп (птицы, яйца, оплодотворённые яйца, инкубатор).
Открывается после полной прокачки овощной фермы (farm.py) и покупки за COOP_COST.
Подключение в main.py:  import coop;  await coop.init() в lifespan;  coop.mount(app, _api_user)
"""
import os
import random

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

import stats
from farm import FARMS, HERE, PER_FARM, RARITY, GardenError, _LOCK, _add, _beds, _int, _pay, helpers

# ---- курятник: открывается после полной прокачки 3 овощной фермы (все 3 фермы и все 36 грядок)
COOP_COST = 50000       # цена открытия курятника (после полной прокачки фермы)
COOP_CAP = 6            # мест в курятнике
EGG_CAP = 10            # сколько яиц копит одна птица
INCUBATORS = 2
HATCH_S = 900           # вылупление, сек
FERT_CHANCE = 0.10      # шанс, что яйцо оплодотворено
QUALITY = ["Обычное", "Хорошее", "Отличное", "Идеальное"]
Q_WEIGHTS = [70, 20, 8, 2]
BASE_W = {"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 1, "mythic": 0.2}
# id, значок, название, редкость, цена, раз в N сек яйцо, цена яйца
_BIRDS = [
    ("chicken", "🐔", "Курица", "common", 3000, 600, 400),
    ("duck", "🦆", "Утка", "common", 8000, 900, 1300),
    ("quail", "🐦", "Перепёлка", "uncommon", 20000, 1200, 4000),
    ("goose", "🪿", "Гусь", "uncommon", 50000, 1800, 12000),
    ("turkey", "🦃", "Индюк", "rare", 120000, 2700, 40000),
    ("peacock", "🦚", "Павлин", "epic", 400000, 3600, 180000),
    ("flamingo", "🦩", "Фламинго", "legendary", 1200000, 5400, 700000),
    ("owl", "🦉", "Золотая сова", "mythic", 5000000, 7200, 4000000),
]
BIRDS = [dict(id=b[0], icon=b[1], name=b[2], rarity=b[3], price=b[4], every=b[5], egg=b[6]) for b in _BIRDS]
BIRD = {b["id"]: b for b in BIRDS}
_RIDX = {r: i for i, r in enumerate(RARITY)}


async def init():
    p = stats.pool
    if not p:
        return
    await p.execute("CREATE TABLE IF NOT EXISTS coop_birds (tg_id BIGINT, slot INT, bird TEXT NOT NULL, "
                    "last TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (tg_id, slot))")
    await p.execute("CREATE TABLE IF NOT EXISTS coop_incubator (tg_id BIGINT, slot INT, quality INT NOT NULL, "
                    "ready TIMESTAMPTZ NOT NULL, PRIMARY KEY (tg_id, slot))")


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
    return {
        "cap": COOP_CAP, "egg_cap": EGG_CAP, "incs": INCUBATORS, "hatch_s": HATCH_S, "quality": QUALITY,
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


async def buybird(uid, b):
    bd = BIRD.get(b.get("bird"))
    if not bd:
        raise GardenError("Неверный запрос")
    async with stats.pool.acquire() as c, c.transaction():
        await _need_coop(c, uid)
        slot = await _free_slot(c, uid, "coop_birds", COOP_CAP, "Курятник полон")
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
        slot = await _free_slot(c, uid, "coop_birds", COOP_CAP, "Курятник полон: освободите место")
        q = await c.fetchval("DELETE FROM coop_incubator WHERE tg_id=$1 AND slot=$2 AND ready<=now() RETURNING quality",
                             uid, _int(b.get("slot"), -1))
        if q is None:
            raise GardenError("Ещё не вылупилось")
        k = 1 + 2 * (q - 1)
        bird = random.choices(BIRDS, [BASE_W[x["rarity"]] * (k if _RIDX[x["rarity"]] >= 2 else 1) for x in BIRDS])[0]
        await c.execute("INSERT INTO coop_birds (tg_id, slot, bird) VALUES ($1,$2,$3)", uid, slot, bird["id"])
    return {"bird": bird["id"]}


COOP_ACTIONS = {
    "unlock": unlock, "buybird": buybird, "sellbird": sellbird, "collect": collect, "sell_eggs": sell_eggs,
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
