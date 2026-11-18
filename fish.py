"""Рыбалка: отдельный мини-апп (3 водоёма, 4 снасти по 30 уровней, 5 наживок, 45 рыб, сеть, сундуки, кит и босс Кракен).
Ловля активная: заброс, поклёвка (нужно подсечь в окно), потяжки; все тайминги проверяет сервер.
Открывается после полной прокачки мясокомбината (meat.py) и покупки за FISH_COST.
Подключение в main.py:  import fish;  await fish.init() в lifespan;  fish.mount(app, _api_user)
"""
import os
import random

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

import stats
from farm import HERE, RARITY, GardenError, _LOCK, _add, _int, _pay, helpers

FISH_COST = 200000                    # цена открытия рыбалки (после полной прокачки мясокомбината)
POND_COSTS = {1: 100000, 2: 500000}   # покупка 2-го и 3-го водоёма (ключ: сколько водоёмов уже открыто)
POND_REQ = {1: 10, 2: 20}             # все четыре снасти не ниже этого уровня
MAX_LV = 30
NET_COST = 25000
NET_USES = (5, 10)                    # сеть рвётся после случайного числа забросов (игрок его не видит)
KRAKEN_P, WHALE_P = 0.0005, 0.001     # шанс на заброс удочкой в море: 0,05% Кракен, 0,1% кит
KRAKEN_LIMIT = 5                      # колоду «Кракен» могут получить только 5 игроков
WHALE_JOKERS = 10                     # кит продаётся за Джокеров
CHEST_P = [("sea", 0.001), ("gold", 0.005), ("rare", 0.02), ("bronze", 0.05)]   # шанс сундука на заброс (морской только в море)
FIGHT = [0, 0, 1, 2, 3, 4]            # потяжек до подсечки по редкости рыбы (минус уровень барабана // 10)
BOSS_HP = 30
RKEYS = list(RARITY)                  # common ... mythic

# id, значок, название, цена наживки
BAITS = [("worm", "🪱", "Червь", 10), ("bread", "🍞", "Хлеб", 40), ("hopper", "🦗", "Кузнечик", 150),
         ("shrimp", "🦐", "Креветка", 600), ("fly", "✨", "Золотая мушка", 2500)]
BAIT_IDX = {b[0]: i for i, b in enumerate(BAITS)}
PONDS = [("🪷", "Пруд"), ("🏞", "Река"), ("🌅", "Море")]
# на каждую наживку в каждом водоёме ловятся 3 вида рыб (чем выше уровень удилища, тем чаще третий, самый редкий)
NAMES = [
    [["Карась", "Плотва", "Окунь"], ["Лещ", "Ротан", "Линь"], ["Карп", "Язь", "Голавль"],
     ["Щука", "Судак", "Сом"], ["Форель", "Золотой карп", "Белый амур"]],
    [["Уклейка", "Пескарь", "Гольян"], ["Подуст", "Чехонь", "Жерех"], ["Налим", "Хариус", "Таймень"],
     ["Лосось", "Стерлядь", "Осётр"], ["Белуга", "Калуга", "Радужный осётр"]],
    [["Килька", "Хамса", "Сардина"], ["Скумбрия", "Тунец", "Камбала"], ["Морской окунь", "Барракуда", "Мурена"],
     ["Акула", "Меч-рыба", "Марлин"], ["Скат-манта", "Рыба-луна", "Золотой тунец"]],
]
FISH_BASE = [30, 100, 380, 1500, 6000, 25000]   # цена рыбы по редкости в 1-м водоёме
POND_MUL = [1, 2.5, 6]
ICONS = ["🐟", "🐠", "🐡", "🦈", "🐙"]
FISH = {}
for _p in range(3):
    for _b in range(5):
        for _s in range(3):
            _r = min(5, _b + (1 if _s == 2 else 0))
            _id = f"{_p}{_b}{_s}"
            FISH[_id] = dict(id=_id, name=NAMES[_p][_b][_s], icon=ICONS[_b], rarity=RKEYS[_r], r=_r, pond=_p, bait=_b,
                             slot=_s, price=int(FISH_BASE[_r] * POND_MUL[_p]))
FISH["whale"] = dict(id="whale", name="Кит", icon="🐋", rarity="mythic", r=5, pond=2, bait=4, slot=3, price=0)

# ключ: (название, значок, цена 1->2 уровня, названия пяти ступеней по 6 уровней: внешний вид меняется)
TACKLE = {
    "rod": ("Удилище", "🎣", 500, ["Бамбуковое", "Стеклопластиковое", "Углепластиковое", "Титановое", "Золотое"]),
    "line": ("Леска", "🧵", 400, ["Простая нить", "Монофильная", "Флюорокарбоновая", "Плетёная", "Золотая нить"]),
    "reel": ("Барабан", "⚙️", 400, ["Простой", "Инерционный", "Безынерционный", "Мультипликатор", "Золотой"]),
    "hooks": ("Крючки", "🪝", 600, ["Простые", "Стальные", "Титановые", "Заточенные", "Золотые"]),
}


def up_cost(key, lv):
    """Цена улучшения снасти с уровня lv на lv+1."""
    return int(round(TACKLE[key][2] * 1.25 ** (lv - 1), -1))


def fish_price(fid, hooks):
    return int(FISH[fid]["price"] * (1 + 0.02 * hooks))


def _phase(hp):
    return 0 if hp > BOSS_HP * 2 / 3 else 1 if hp > BOSS_HP / 3 else 2


def _pull_win(P, boss_hp=None):
    w = 1.1 + 0.03 * P["line"]
    return w * (1.0, 0.8, 0.6)[_phase(boss_hp)] if boss_hp is not None else w


async def init():
    p = stats.pool
    if not p:
        return
    await p.execute("CREATE TABLE IF NOT EXISTS fish_player (tg_id BIGINT PRIMARY KEY, open INT NOT NULL DEFAULT 0, "
                    "ponds INT NOT NULL DEFAULT 1, rod INT NOT NULL DEFAULT 1, line INT NOT NULL DEFAULT 1, "
                    "reel INT NOT NULL DEFAULT 1, hooks INT NOT NULL DEFAULT 1, net_left INT NOT NULL DEFAULT 0)")
    await p.execute("CREATE TABLE IF NOT EXISTS fish_cast (tg_id BIGINT PRIMARY KEY, state TEXT NOT NULL, pond INT NOT NULL, "
                    "bait TEXT, bite_at TIMESTAMPTZ, win DOUBLE PRECISION NOT NULL DEFAULT 2, fish TEXT, "
                    "rounds INT NOT NULL DEFAULT 0, rnd INT NOT NULL DEFAULT 0, pull_at TIMESTAMPTZ, "
                    "pull_win DOUBLE PRECISION NOT NULL DEFAULT 1.5, hp INT NOT NULL DEFAULT 0, "
                    "maxhp INT NOT NULL DEFAULT 0, line_hp INT NOT NULL DEFAULT 0)")


_CQ = ("SELECT *, EXTRACT(EPOCH FROM (bite_at-now()))::float AS bite_in, "
       "EXTRACT(EPOCH FROM (pull_at-now()))::float AS pull_in FROM fish_cast WHERE tg_id=$1 FOR UPDATE")


def _meat_done(g):
    return g["mp_pages"] >= 3 and g["mp3"] >= 9


async def _player(c, uid):
    await c.execute("INSERT INTO fish_player (tg_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
    return await c.fetchrow("SELECT * FROM fish_player WHERE tg_id=$1", uid)


async def _need_fish(c, uid):
    await c.fetchval(_LOCK, uid)
    g = await c.fetchrow("SELECT mp_pages, mp3 FROM garden WHERE tg_id=$1", uid)
    if not _meat_done(g):
        raise GardenError("Рыбалка заблокирована: прокачайте мясокомбинат до конца")
    P = await _player(c, uid)
    if P["open"] < 1:
        raise GardenError(f"Сначала откройте рыбалку за {FISH_COST} ♠️")
    return P


async def _expire(c, uid):
    """Подводит итоги пропущенных моментов. Возвращает (текущий заброс или None, сообщение или None)."""
    r = await c.fetchrow(_CQ, uid)
    if not r:
        return None, None
    if r["state"] == "wait" and r["bite_in"] < -r["win"] - 0.3:
        await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
        return None, "Рыба сорвалась: вы не успели подсечь"
    if r["state"] == "fight" and r["pull_in"] < -r["pull_win"] - 0.3:
        if r["fish"] != "kraken":
            await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
            return None, "Рыба сорвалась: вы не успели потянуть"
        lh = r["line_hp"] - 1
        if lh <= 0:
            await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
            return None, "Кракен порвал леску и ушёл на глубину"
        P = await c.fetchrow("SELECT * FROM fish_player WHERE tg_id=$1", uid)
        await c.execute("UPDATE fish_cast SET line_hp=$2, pull_at=now()+make_interval(secs=>$3), pull_win=$4 WHERE tg_id=$1",
                        uid, lh, random.uniform(1.0, 2.0), _pull_win(P, r["hp"]))
        return await c.fetchrow(_CQ, uid), "Промах: леска ослабла"
    return r, None


async def fish_view(uid):
    """Состояние мини-аппа: закрыт (прогресс мясокомбината), можно открыть или открыт (вся рыбалка)."""
    async with stats.pool.acquire() as c, c.transaction():
        await c.fetchval(_LOCK, uid)
        g = await c.fetchrow("SELECT mp_pages, mp1, mp2, mp3 FROM garden WHERE tg_id=$1", uid)
        me = await c.fetchrow("SELECT coins, jokers FROM players WHERE tg_id=$1", uid)
        P = await _player(c, uid)
        out = {
            "unlocked": _meat_done(g) and P["open"] >= 1, "meat_done": _meat_done(g), "fish_cost": FISH_COST,
            "meat_pages": g["mp_pages"], "meat_cells": g["mp1"] + g["mp2"] + g["mp3"], "meat_total": 27,
            "coins": me["coins"] if me else 0, "jokers": me["jokers"] if me else 0,
            "rarity": {k: list(v[:2]) for k, v in RARITY.items()},
        }
        if not out["unlocked"]:
            return out
        r, note = await _expire(c, uid)
        items = await c.fetch("SELECT kind, item, qty FROM garden_items WHERE tg_id=$1 AND kind IN ('bait','fish') AND qty>0", uid)
        lv = {k: P[k] for k in TACKLE}
        low = min(lv.values())
        cast = None
        if r:
            cast = {"state": r["state"], "pond": r["pond"], "bait": r["bait"], "bite_in": r["bite_in"], "win": r["win"],
                    "fish": r["fish"], "rounds": r["rounds"], "rnd": r["rnd"], "pull_in": r["pull_in"],
                    "pull_win": r["pull_win"], "hp": r["hp"], "maxhp": r["maxhp"], "line_hp": r["line_hp"]}
        out["fish"] = {
            "ponds": P["ponds"], "ponds_cat": [list(x) for x in PONDS], "pond_cost": POND_COSTS.get(P["ponds"], 0),
            "pond_req": POND_REQ.get(P["ponds"], 0), "pond_ready": P["ponds"] < 3 and low >= POND_REQ.get(P["ponds"], 99),
            "lv": lv, "max_lv": MAX_LV, "up_cost": {k: up_cost(k, lv[k]) if lv[k] < MAX_LV else 0 for k in TACKLE},
            "tackle": {k: {"name": v[0], "icon": v[1], "tiers": v[3]} for k, v in TACKLE.items()},
            "baits": [dict(id=b[0], icon=b[1], name=b[2], price=b[3]) for b in BAITS],
            "bait": {i["item"]: i["qty"] for i in items if i["kind"] == "bait"},
            "fish": {i["item"]: i["qty"] for i in items if i["kind"] == "fish"},
            "catalog": list(FISH.values()), "net": P["net_left"] > 0, "net_cost": NET_COST,
            "hooks_mul": 1 + 0.02 * P["hooks"], "whale_jokers": WHALE_JOKERS,
            "cast": cast, "note": note,
        }
        return out


# ----------------------------------------------------------------- действия

async def unlock(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        await c.fetchval(_LOCK, uid)
        g = await c.fetchrow("SELECT mp_pages, mp3 FROM garden WHERE tg_id=$1", uid)
        if not _meat_done(g):
            raise GardenError("Сначала прокачайте мясокомбинат до конца")
        P = await _player(c, uid)
        if P["open"] >= 1:
            raise GardenError("Рыбалка уже открыта")
        await _pay(c, uid, FISH_COST)
        await c.execute("UPDATE fish_player SET open=1 WHERE tg_id=$1", uid)
    return {}


async def buybait(uid, b):
    bid, qty = b.get("bait"), _int(b.get("qty"), 1)
    if bid not in BAIT_IDX or not 1 <= qty <= 99:
        raise GardenError("Неверный запрос")
    async with stats.pool.acquire() as c, c.transaction():
        await _need_fish(c, uid)
        await _pay(c, uid, BAITS[BAIT_IDX[bid]][3] * qty)
        await _add(c, uid, "bait", bid, qty)
    return {}


async def upgrade(uid, b):
    key = b.get("tackle")
    if key not in TACKLE:
        raise GardenError("Неверный запрос")
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        if P[key] >= MAX_LV:
            raise GardenError("Максимальный уровень")
        await _pay(c, uid, up_cost(key, P[key]))
        await c.execute(f"UPDATE fish_player SET {key}={key}+1 WHERE tg_id=$1", uid)
    return {}


async def buypond(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        if P["ponds"] >= 3:
            raise GardenError("Все водоёмы уже открыты")
        need = POND_REQ[P["ponds"]]
        if min(P[k] for k in TACKLE) < need:
            raise GardenError(f"Сначала прокачайте все снасти до {need} уровня")
        await _pay(c, uid, POND_COSTS[P["ponds"]])
        await c.execute("UPDATE fish_player SET ponds=ponds+1 WHERE tg_id=$1", uid)
    return {}


async def buynet(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        if P["net_left"] > 0:
            raise GardenError("Прежняя сеть ещё цела")
        await _pay(c, uid, NET_COST)
        await c.execute("UPDATE fish_player SET net_left=$2 WHERE tg_id=$1", uid, random.randint(*NET_USES))
    return {}


async def cast(uid, b):
    pond, bait = _int(b.get("pond"), -1), b.get("bait")
    if bait not in BAIT_IDX:
        raise GardenError("Неверный запрос")
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        if not 0 <= pond < P["ponds"]:
            raise GardenError("Этот водоём ещё закрыт")
        r, _ = await _expire(c, uid)
        if r:
            raise GardenError("Удочка уже заброшена")
        left = await c.fetchval("UPDATE garden_items SET qty=qty-1 WHERE tg_id=$1 AND kind='bait' AND item=$2 AND qty>0 "
                                "RETURNING qty", uid, bait)
        if left is None:
            raise GardenError("Нет такой наживки: купите её")
        wait = random.uniform(4.0, 9.0) * (1 - 0.015 * P["reel"])
        await c.execute("INSERT INTO fish_cast (tg_id, state, pond, bait, bite_at, win) VALUES "
                        "($1,'wait',$2,$3, now()+make_interval(secs=>$4), $5)", uid, pond, bait, wait, 1.6 + 0.02 * P["line"])
    return {}


async def _add_chest(c, uid, kind):
    await c.execute("INSERT INTO player_chests (tg_id, kind, qty) VALUES ($1,$2,1) "
                    "ON CONFLICT (tg_id, kind) DO UPDATE SET qty=player_chests.qty+1", uid, kind)


async def _kraken_free(c, uid):
    n = await c.fetchval("SELECT count(*) FROM player_items WHERE item='deck_kraken'")
    mine = await c.fetchval("SELECT 1 FROM player_items WHERE tg_id=$1 AND item='deck_kraken'", uid)
    return n < KRAKEN_LIMIT and not mine


async def hook(uid, b):
    """Подсечка: удачна только в окне поклёвки. Решает, что на крючке: Кракен, кит, сундук или рыба."""
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        r = await c.fetchrow(_CQ, uid)
        if not r or r["state"] != "wait":
            raise GardenError("Сейчас нечего подсекать")
        if r["bite_in"] > 0.15:
            await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
            raise GardenError("Слишком рано: рыба испугалась")
        if r["bite_in"] < -r["win"] - 0.2:
            await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
            raise GardenError("Рыба сорвалась: вы не успели")
        pond = r["pond"]
        if pond == 2 and random.random() < KRAKEN_P and await _kraken_free(c, uid):
            lh = 3 + P["line"] // 6
            await c.execute("UPDATE fish_cast SET state='fight', fish='kraken', rounds=0, rnd=0, hp=$2, maxhp=$2, line_hp=$3, "
                            "pull_at=now()+make_interval(secs=>$4), pull_win=$5 WHERE tg_id=$1",
                            uid, BOSS_HP, lh, random.uniform(1.0, 2.0), _pull_win(P, BOSS_HP))
            return {"event": "kraken"}
        if pond == 2 and random.random() < WHALE_P:
            await _add(c, uid, "fish", "whale", 1)
            await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
            return {"event": "whale"}
        mul = 1 + 0.03 * P["rod"]
        for kind, pr in CHEST_P:
            if kind == "sea" and pond != 2:
                continue
            if random.random() < pr * mul:
                await _add_chest(c, uid, kind)
                await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
                return {"event": "chest", "chest": kind}
        bi = BAIT_IDX[r["bait"]]
        slot = random.choices([0, 1, 2], [60, 30, 10 * (1 + 0.1 * P["rod"])])[0]
        fid = f"{pond}{bi}{slot}"
        k = max(0, FIGHT[FISH[fid]["r"]] - P["reel"] // 10)
        if k == 0:
            await _add(c, uid, "fish", fid, 1)
            await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
            return {"event": "fish", "fish": fid}
        await c.execute("UPDATE fish_cast SET state='fight', fish=$2, rounds=$3, rnd=0, "
                        "pull_at=now()+make_interval(secs=>$4), pull_win=$5 WHERE tg_id=$1",
                        uid, fid, k, random.uniform(1.0, 2.2), _pull_win(P))
        return {"event": "fight", "fish": fid}


async def pull(uid, b):
    """Потяжка: нужно нажать в окне. Мимо: у обычной рыбы она уходит, у Кракена ослабляется леска."""
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        r = await c.fetchrow(_CQ, uid)
        if not r or r["state"] != "fight":
            raise GardenError("Сейчас нечего тянуть")
        boss = r["fish"] == "kraken"
        if not (-r["pull_win"] - 0.2 <= r["pull_in"] <= 0.15):      # допуск на задержку сети
            if not boss:
                await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
                raise GardenError("Рыба сорвалась: потяжка не вовремя")
            lh = r["line_hp"] - 1
            if lh <= 0:
                await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
                raise GardenError("Кракен порвал леску и ушёл на глубину")
            await c.execute("UPDATE fish_cast SET line_hp=$2, pull_at=now()+make_interval(secs=>$3) WHERE tg_id=$1",
                            uid, lh, random.uniform(1.0, 2.0))
            return {"event": "miss"}
        if not boss:
            rnd = r["rnd"] + 1
            if rnd >= r["rounds"]:
                await _add(c, uid, "fish", r["fish"], 1)
                await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
                return {"event": "fish", "fish": r["fish"]}
            await c.execute("UPDATE fish_cast SET rnd=$2, pull_at=now()+make_interval(secs=>$3) WHERE tg_id=$1",
                            uid, rnd, random.uniform(1.0, 2.2))
            return {"event": "pull"}
        dmg = 2 + P["rod"] // 3
        crit = random.random() < 0.01 * P["hooks"] * (2 if _phase(r["hp"]) == 2 else 1)
        hp = r["hp"] - dmg * (2 if crit else 1)
        if hp <= 0:
            await c.execute("SELECT pg_advisory_xact_lock(7770001)")
            got = await _kraken_free(c, uid)
            if got:
                await c.execute("INSERT INTO player_items (tg_id, item) VALUES ($1,'deck_kraken') ON CONFLICT DO NOTHING", uid)
            else:
                await c.execute("UPDATE players SET jokers=jokers+5 WHERE tg_id=$1", uid)
            await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
            return {"event": "kraken_win", "got": got}
        await c.execute("UPDATE fish_cast SET hp=$2, pull_at=now()+make_interval(secs=>$3), pull_win=$4 WHERE tg_id=$1",
                        uid, hp, random.uniform(1.0, 2.0), _pull_win(P, hp))
        return {"event": "hit", "crit": crit}


async def reel_in(uid, b):
    """Смотать удочку: наживка пропадает."""
    async with stats.pool.acquire() as c, c.transaction():
        await _need_fish(c, uid)
        await c.execute("DELETE FROM fish_cast WHERE tg_id=$1", uid)
    return {}


async def usenet(uid, b):
    """Сеть: без поклёвки, 3-6 обычных и необычных рыб (больше с уровнем лески), редкой рыбы, кита и сундуков нет."""
    pond = _int(b.get("pond"), -1)
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        if not 0 <= pond < P["ponds"]:
            raise GardenError("Этот водоём ещё закрыт")
        if P["net_left"] <= 0:
            raise GardenError("У вас нет целой сети: купите новую")
        ids = [f"{pond}00", f"{pond}01", f"{pond}02", f"{pond}10", f"{pond}11"]
        n = random.randint(3, 6) + P["line"] // 10
        got = {}
        for _ in range(n):
            fid = random.choices(ids, [50, 50, 25, 25, 25])[0]
            got[fid] = got.get(fid, 0) + 1
        for fid, q in got.items():
            await _add(c, uid, "fish", fid, q)
        left = P["net_left"] - 1
        await c.execute("UPDATE fish_player SET net_left=$2 WHERE tg_id=$1", uid, left)
    return {"event": "net", "n": n, "broke": left <= 0}


async def sell(uid, b):
    async with stats.pool.acquire() as c, c.transaction():
        P = await _need_fish(c, uid)
        rows = await c.fetch("DELETE FROM garden_items WHERE tg_id=$1 AND kind='fish' RETURNING item, qty", uid)
        coins = sum(fish_price(r["item"], P["hooks"]) * r["qty"] for r in rows if r["item"] in FISH and r["item"] != "whale")
        whales = sum(r["qty"] for r in rows if r["item"] == "whale")
        if not coins and not whales:
            raise GardenError("Нет улова на продажу")
        await c.execute("UPDATE players SET coins=coins+$2, jokers=jokers+$3 WHERE tg_id=$1", uid, coins, whales * WHALE_JOKERS)
    return {"coins": coins, "jokers": whales * WHALE_JOKERS}


ACTIONS = {
    "unlock": unlock, "buybait": buybait, "upgrade": upgrade, "buypond": buypond, "buynet": buynet, "cast": cast,
    "hook": hook, "pull": pull, "reel_in": reel_in, "usenet": usenet, "sell": sell,
}


def mount(app, auth):
    uid_of, run = helpers(auth)

    @app.get("/fish")
    async def fish_page():
        return FileResponse(os.path.join(HERE, "fish.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/api/fish")
    async def fish_get(request: Request):
        return await fish_view(await uid_of(request))

    @app.post("/api/fish/{act}")
    async def fish_do(act: str, request: Request):
        if act not in ACTIONS:
            raise HTTPException(status_code=404)
        return await run(request, ACTIONS[act], fish_view)
