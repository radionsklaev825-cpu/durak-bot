import os, random, asyncpg
from datetime import datetime, timedelta, timezone

pool = None

# Валюты:  ♠️ Козыри (coins) — обычная, зарабатывается в играх, на колесе и за достижения
#          🃏 Джокеры (jokers) — донатная (покупается за Telegram Stars)
SPIN_COOLDOWN = timedelta(hours=24)


# ---------------------------------------------------------------- лиги
# 10 лиг в стиле казино и азарта: от «Счастливчика» до «Короля Вегаса».
# (значок, название, рейтинг для входа, «подушка»)
# Подушка: попав в лигу, игрок остаётся в ней, пока рейтинг не упадёт ниже (порог - подушка).
# Шаг между лигами растёт: первые лиги проходятся быстро, дальше всё дольше.
LEAGUES = [
    ("🍀", "Счастливчик",      0,     0),
    ("🔥", "Азартный",      1500,   100),
    ("💎", "Рисковый",      2500,   100),
    ("🎰", "Везунчик",      3700,   150),
    ("💰", "Крупный игрок", 5100,   150),
    ("🥂", "Хайроллер",     6700,   200),
    ("🏆", "Чемпион стола", 8500,   200),
    ("👑", "Хозяин казино", 10500,  250),
    ("⚡", "Легенда азарта", 12700,  250),
    ("🌟", "Король Вегаса", 15000,  300),
]


def league_for(rating):
    """Лига (индекс 0-9) по рейтингу без учёта подушки."""
    idx = 0
    for i, (_i, _n, thr, _c) in enumerate(LEAGUES):
        if rating >= thr:
            idx = i
    return idx


def league_of(rating, stored=None):
    """Текущая лига игрока: сохранённая, а если её ещё нет (старые игроки), то по рейтингу."""
    return stored if stored is not None else league_for(rating)


def update_league(cur, rating):
    """Новая лига после изменения рейтинга: вверх по порогу, вниз только за подушкой."""
    while cur + 1 < len(LEAGUES) and rating >= LEAGUES[cur + 1][2]:
        cur += 1
    while cur > 0 and rating < LEAGUES[cur][2] - LEAGUES[cur][3]:
        cur -= 1
    return cur


def league_title(idx):
    return f"{LEAGUES[idx][0]} {LEAGUES[idx][1]}"


# Награда за первый выход в лигу (один раз, повторно не выдаётся):
# (Козыри, Джокеры, [сундуки], эксклюзивный предмет или None). С каждой лигой награды заметно крупнее.
LEAGUE_REWARDS = [
    (0,      0, [],                         None),              # 1 Счастливчик: стартовая лига
    (200,    0, [],                         None),              # 2 Азартный
    (500,    0, ["bronze"],                 None),              # 3 Рисковый
    (1000,   1, ["bronze"],                 None),              # 4 Везунчик
    (2000,   2, ["rare"],                   None),              # 5 Крупный игрок
    (4000,   3, ["rare"],                   "deck_chips"),      # 6 Хайроллер
    (7000,   5, ["gold"],                   "table_hall"),      # 7 Чемпион стола
    (12000,  8, ["gold"],                   "deck_blackgold"),  # 8 Хозяин казино
    (20000, 12, ["gold", "gold"],           "table_lightning"), # 9 Легенда азарта
    (50000, 25, ["gold", "gold", "gold"],   "deck_crown"),      # 10 Король Вегаса
]


def league_reward_text(idx):
    coins, jokers, chests, item = LEAGUE_REWARDS[idx]
    parts = []
    if coins:
        parts.append(f"+{coins:,} ♠️".replace(",", " "))
    if jokers:
        parts.append(f"+{jokers} 🃏")
    for k in chests:
        parts.append(f"{CHESTS[k][0]}")
    if item:
        parts.append(f"{ITEMS[item][3]} {ITEMS[item][2]} ({'колода' if ITEMS[item][0] == 'deck' else 'стол'})")
    return " ".join(parts) or "без награды"

# (название, валюта, количество, вес шанса)
WHEEL = [
    ("20 ♠️ Козырей",   "coins",  20, 38),
    ("50 ♠️ Козырей",   "coins",  50, 28),
    ("100 ♠️ Козырей",  "coins", 100, 16),
    ("250 ♠️ Козырей",  "coins", 250,  9),
    ("500 ♠️ Козырей",  "coins", 500,  4),
    ("1 🃏 Джокер",     "jokers",  1,  4),
    ("5 🃏 Джокеров",   "jokers",  5,  1),
]

# id: (значок, название, как получить, награда Козырями, награда Джокерами, условие)
# условие получает строку игрока r и набор событий e ("spin", "jackpot", "purchase")
ACH = {
    "first_win":   ("🥇", "Первая победа",    "Выиграйте партию",              50, 0, lambda r, e: r["wins"] >= 1),
    "wins_10":     ("🏆", "Десять побед",     "Выиграйте 10 партий",          100, 0, lambda r, e: r["wins"] >= 10),
    "wins_50":     ("👑", "Мастер",           "Выиграйте 50 партий",          300, 1, lambda r, e: r["wins"] >= 50),
    "wins_100":    ("💎", "Легенда",          "Выиграйте 100 партий",         500, 3, lambda r, e: r["wins"] >= 100),
    "games_10":    ("🎴", "Освоился",         "Сыграйте 10 партий",            30, 0, lambda r, e: r["wins"] + r["losses"] >= 10),
    "games_100":   ("🃏", "Ветеран",          "Сыграйте 100 партий",          200, 0, lambda r, e: r["wins"] + r["losses"] >= 100),
    "streak_3":    ("🔥", "Серия",            "3 победы подряд",               50, 0, lambda r, e: (r["win_streak"] or 0) >= 3),
    "streak_5":    ("⚡", "Неудержимый",      "5 побед подряд",               150, 0, lambda r, e: (r["win_streak"] or 0) >= 5),
    "rating_1200": ("📈", "Растём",           "Достигните рейтинга 1200",     100, 0, lambda r, e: r["rating"] >= 1200),
    "rating_1500": ("🚀", "Элита",            "Достигните рейтинга 1500",     300, 1, lambda r, e: r["rating"] >= 1500),
    "first_spin":  ("🎡", "Испытал удачу",    "Крутните колесо фортуны",       20, 0, lambda r, e: "spin" in e),
    "jackpot":     ("🎰", "Джекпот",          "Выбейте 5 Джокеров на колесе", 100, 0, lambda r, e: "jackpot" in e),
    "patron":      ("💖", "Меценат",          "Купите Джокеров",               50, 0, lambda r, e: "purchase" in e),
    "first_chest": ("📦", "Первый сундук",    "Откройте сундук",               30, 0, lambda r, e: "chest" in e),
    "legendary":   ("🌟", "Легендарная находка", "Выбейте легендарный предмет", 150, 0, lambda r, e: "legendary" in e),
    "collector":   ("🗂️", "Коллекционер",     "Соберите 8 предметов",         100, 1, lambda r, e: "items8" in e),
}


# ---------------------------------------------------------------- сундуки и косметика
# 3 редкости. Предметы только внешние: на игру и рейтинг не влияют.
RARITY = {"common": ("⚪", "Обычная"), "rare": ("🔵", "Редкая"), "legendary": ("🟡", "Легендарная"),
          "mythic": ("🟣", "Мифическая"),   # мифические не лежат в сундуках: выпадают только за победу
          "league": ("🟠", "Лиговая"),      # только за выход в лигу
          "secret": ("🔴", "Секрет")}       # только за особое условие (см. SECRET_NEED)

# id: (тип, редкость, название, значок). Тип: "deck" (рубашка карт) или "table" (стол)
ITEMS = {
    "deck_classic":   ("deck",  "common",    "Классика",         "🂠"),
    "table_classic":  ("table", "common",    "Зелёное сукно",    "🟩"),
    # обычные
    "deck_blue":      ("deck",  "common",    "Синяя рубашка",    "🟦"),
    "deck_red":       ("deck",  "common",    "Красная рубашка",  "🟥"),
    "deck_graphite":  ("deck",  "common",    "Графит",           "⬛"),
    "table_blue":     ("table", "common",    "Синее сукно",      "🟦"),
    "table_burgundy": ("table", "common",    "Бордо",            "🍷"),
    "table_slate":    ("table", "common",    "Серый камень",     "🪨"),
    # редкие
    "deck_gold":      ("deck",  "rare",      "Золотой узор",     "✨"),
    "deck_neon":      ("deck",  "rare",      "Неон",             "💠"),
    "deck_east":      ("deck",  "rare",      "Восточный орнамент", "🏮"),
    "table_wood":     ("table", "rare",      "Тёмное дерево",    "🪵"),
    "table_marble":   ("table", "rare",      "Мрамор",           "🏛️"),
    "table_city":     ("table", "rare",      "Ночной город",     "🌃"),
    # легендарные
    "deck_dragon":    ("deck",  "legendary", "Дракон",           "🐉"),
    "deck_cosmos":    ("deck",  "legendary", "Космос",           "🌌"),
    "table_vegas":    ("table", "legendary", "Казино Вегас",     "🎰"),
    "table_stars":    ("table", "legendary", "Звёздное небо",    "🌠"),
    # мифические
    "deck_phoenix":   ("deck",  "mythic",    "Феникс",           "🔥"),
    "table_blackhole": ("table", "mythic",    "Чёрная дыра",      "🕳️"),
    # лиговые: выдаются только за первый выход в лигу (в сундуках и за победы их нет)
    "deck_chips":      ("deck",  "league",    "Фишки хайроллера", "🪙"),
    "table_hall":      ("table", "league",    "Зал чемпионов",    "🏆"),
    "deck_blackgold":  ("deck",  "league",    "Чёрное золото",    "🖤"),
    "table_lightning": ("table", "league",    "Золотая молния",   "⚡"),
    "deck_crown":      ("deck",  "league",    "Корона Вегаса",    "👑"),
    # секретные
    "deck_vegas":      ("deck",  "secret",    "Леди Вегаса",      "💃"),
}
DEFAULTS = {"deck": "deck_classic", "table": "table_classic"}  # есть у всех
POOL = {r: [i for i, v in ITEMS.items() if v[1] == r and i not in DEFAULTS.values()]
        for r in RARITY}

# id: (значок, название, валюта, цена, шансы редкостей в %)
CHESTS = {
    "bronze": ("📦", "Обычный сундук",  "coins",  150, {"common": 75, "rare": 22, "legendary": 3}),
    "rare":   ("💠", "Редкий сундук",   "coins",  500, {"common": 40, "rare": 50, "legendary": 10}),
    "gold":   ("🎁", "Золотой сундук",  "jokers",   3, {"common": 0,  "rare": 70, "legendary": 30}),
}
# шанс (в %) получить сундук за победу в партии; проверяется один раз: сначала золотой, потом редкий, потом обычный
DROP_CHANCE = {"gold": 1.0, "rare": 3.0, "bronze": 7.0}

# если предмет уже есть, возвращаем Козырями
DUP_REFUND = {"common": 20, "rare": 60, "legendary": 200, "mythic": 1000, "league": 0, "secret": 0}

# шанс (в %) выбить мифический предмет за победу. 0.05% = примерно 1 раз на 2000 побед.
# Для проверки можно временно поставить MYTHIC_CHANCE=100 в переменных окружения.
MYTHIC_CHANCE = float(os.getenv("MYTHIC_CHANCE") or 0.05)


def item_title(iid):
    kind, rar, name, icon = ITEMS[iid]
    return f"{RARITY[rar][0]} {icon} {name} ({'колода' if kind == 'deck' else 'стол'})"


def _reward(coins, jokers):
    parts = []
    if coins:
        parts.append(f"+{coins} ♠️")
    if jokers:
        parts.append(f"+{jokers} 🃏")
    return " ".join(parts)


def describe(aid):
    if aid == "@" + SECRET_ITEM:
        return "🔴 СЕКРЕТНАЯ КОЛОДА открыта: «Леди Вегаса»! Надеть: /collection в личке с ботом"
    icon, title, _desc, coins, jokers, _c = ACH[aid]
    return f"{icon} «{title}» ({_reward(coins, jokers)})"


# ---------------------------------------------------------------- эмоции
# 6 базовых эмоций есть у всех. Остальные выпадают из сундука эмоций.
# Мифическая эмоция выпадает только за победу. Рисунки лежат в index.html (объект EMO).
BASE_EMOTES = ["laugh", "angry", "shock", "wink", "gotcha", "crown"]
# id: (редкость, название)
EMOTES = {
    "laugh": ("base", "Смех"), "angry": ("base", "Злость"), "shock": ("base", "Шок"),
    "wink": ("base", "Ловко"), "gotcha": ("base", "Поймал"), "crown": ("base", "Король"),
    "like": ("common", "Класс"), "cry": ("common", "Плачу"), "bored": ("common", "Скучно"),
    "think": ("common", "Думаю"),
    "fire": ("rare", "Огонь"), "fool": ("rare", "Дурак!"), "boss": ("rare", "Босс"),
    "pay": ("rare", "Плати"),
    "devil": ("legendary", "Дьявол"), "jackpot": ("legendary", "Джекпот"),
    "boom": ("legendary", "Взрыв мозга"),
    "ace": ("mythic", "Туз в рукаве"),
}
EMOTE_POOL = {r: [k for k, v in EMOTES.items() if v[0] == r] for r in ("common", "rare", "legendary", "mythic")}
# значок, название, валюта, цена, шансы редкостей в %
EMOTE_CHEST = ("🎭", "Сундук эмоций", "coins", 100, {"common": 60, "rare": 30, "legendary": 10})
EMOTE_DUP_REFUND = {"common": 10, "rare": 30, "legendary": 100, "mythic": 1000}
# шанс (в %) выбить мифическую эмоцию за победу (1% = примерно раз на 100 побед)
MYTHIC_EMOTE_CHANCE = float(os.getenv("MYTHIC_EMOTE_CHANCE") or 1.0)


def emote_title(eid):
    rar, name = EMOTES[eid]
    ic = RARITY[rar][0] if rar in RARITY else "⚪"
    return f"{ic} {name}"


# Секретная колода «Леди Вегаса» выдаётся, когда собраны ВСЕ достижения, кроме платного и удачного.
SECRET_ITEM = "deck_vegas"
SECRET_SKIP = ("patron", "jackpot")
SECRET_NEED = [k for k in ACH if k not in SECRET_SKIP]


START_COINS = 300      # разовая награда: кнопка «Получить вознаграждение» для всех игроков
OWNER_ID = int(os.getenv("OWNER_ID") or 8617345250)        # Telegram ID владельца бота
OWNER_COINS = int(os.getenv("OWNER_COINS") or 1_000_000)   # Козырей у владельца для проверки
OWNER_JOKERS = int(os.getenv("OWNER_JOKERS") or 10_000)    # Джокеров у владельца для проверки

# Разовые подарки: {(Telegram ID, метка подарка): (Козыри, Джокеры, [предметы])}.
# Выдаются ОДИН РАЗ (бот помнит метку в базе) и прибавляются к текущему балансу.
# Чтобы подарить ещё раз, смените метку, например "gift2".
GRANTS = {
    (812057912, "gift1"): (1_000_000, 10_000, ["deck_vegas"]),   # колода «Леди Вегаса»
}


async def init():
    global pool
    url = os.getenv("DATABASE_URL")
    if not url:
        return
    try:
        pool = await asyncpg.create_pool(url, min_size=1, max_size=3,
                                         max_inactive_connection_lifetime=30)
        await pool.execute("""
        CREATE TABLE IF NOT EXISTS players (
            tg_id BIGINT PRIMARY KEY, name TEXT,
            rating INT DEFAULT 1000, coins INT DEFAULT 0,
            wins INT DEFAULT 0, losses INT DEFAULT 0,
            jokers INT DEFAULT 0, last_spin TIMESTAMPTZ,
            win_streak INT DEFAULT 0);
        ALTER TABLE players ADD COLUMN IF NOT EXISTS jokers INT DEFAULT 0;
        ALTER TABLE players ADD COLUMN IF NOT EXISTS last_spin TIMESTAMPTZ;
        ALTER TABLE players ADD COLUMN IF NOT EXISTS win_streak INT DEFAULT 0;
        ALTER TABLE players ADD COLUMN IF NOT EXISTS deck TEXT;
        ALTER TABLE players ADD COLUMN IF NOT EXISTS tbl TEXT;
        ALTER TABLE players ALTER COLUMN coins SET DEFAULT 0;
        ALTER TABLE players ADD COLUMN IF NOT EXISTS bonus_claimed BOOLEAN DEFAULT FALSE;
        ALTER TABLE players ADD COLUMN IF NOT EXISTS league INT;
        ALTER TABLE players ADD COLUMN IF NOT EXISTS league_max INT;
        CREATE TABLE IF NOT EXISTS payments (
            charge_id TEXT PRIMARY KEY, tg_id BIGINT, jokers INT, stars INT,
            created TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS achievements (
            tg_id BIGINT, ach TEXT, created TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (tg_id, ach));
        CREATE TABLE IF NOT EXISTS player_items (
            tg_id BIGINT, item TEXT, PRIMARY KEY (tg_id, item));
        CREATE TABLE IF NOT EXISTS grants_done (
            tag TEXT, tg_id BIGINT, created TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (tag, tg_id));
        CREATE TABLE IF NOT EXISTS player_emotes (
            tg_id BIGINT, emote TEXT, PRIMARY KEY (tg_id, emote));
        CREATE TABLE IF NOT EXISTS player_chests (
            tg_id BIGINT, kind TEXT, qty INT DEFAULT 0, PRIMARY KEY (tg_id, kind));
        """)
    except Exception as e:
        print("db init error:", e)
        pool = None
        return
    if OWNER_ID:   # владелец: держим на счёте не меньше OWNER_COINS (для проверки)
        try:
            await pool.execute(
                "INSERT INTO players (tg_id, name, coins, jokers) VALUES ($1,'Владелец',$2,$3) "
                "ON CONFLICT (tg_id) DO UPDATE SET coins=GREATEST(players.coins,$2), "
                "jokers=GREATEST(players.jokers,$3)",
                OWNER_ID, OWNER_COINS, OWNER_JOKERS)
        except Exception as e:
            print("owner coins error:", e)
    for (gid, tag), (g_coins, g_jokers, g_items) in GRANTS.items():   # разовые подарки
        try:
            async with pool.acquire() as c, c.transaction():
                fresh = await c.fetchval(
                    "INSERT INTO grants_done (tag, tg_id) VALUES ($1,$2) "
                    "ON CONFLICT DO NOTHING RETURNING tag", tag, gid)
                if not fresh:        # этот подарок уже выдавали: ничего не делаем
                    continue
                await c.execute(
                    "INSERT INTO players (tg_id, name, coins, jokers) VALUES ($1,'Игрок',$2,$3) "
                    "ON CONFLICT (tg_id) DO UPDATE SET coins=players.coins+$2, jokers=players.jokers+$3",
                    gid, g_coins, g_jokers)
                for it in g_items:
                    await c.execute("INSERT INTO player_items (tg_id, item) VALUES ($1,$2) "
                                    "ON CONFLICT DO NOTHING", gid, it)
        except Exception as e:
            print("grant error:", gid, e)


async def _grant(c, uid, extra=()):
    """Проверяет достижения игрока, выдаёт новые и награды за них.
    Вызывать внутри транзакции. Возвращает список id новых достижений."""
    row = await c.fetchrow("SELECT * FROM players WHERE tg_id=$1", uid)
    have = {r["ach"] for r in await c.fetch("SELECT ach FROM achievements WHERE tg_id=$1", uid)}
    ev = set(extra)
    new, tc, tj = [], 0, 0
    for aid, (_i, _t, _d, coins, jokers, cond) in ACH.items():
        if aid in have or not cond(row, ev):
            continue
        ins = await c.fetchval(
            "INSERT INTO achievements (tg_id, ach) VALUES ($1,$2) "
            "ON CONFLICT DO NOTHING RETURNING ach", uid, aid)
        if ins:
            new.append(aid)
            tc += coins
            tj += jokers
    if new:
        await c.execute("UPDATE players SET coins=coins+$2, jokers=jokers+$3 WHERE tg_id=$1",
                        uid, tc, tj)
    done = have | set(new)
    if all(k in done for k in SECRET_NEED):      # все нужные достижения собраны: секретная колода
        got = await c.fetchval(
            "INSERT INTO player_items (tg_id, item) VALUES ($1,$2) "
            "ON CONFLICT DO NOTHING RETURNING item", uid, SECRET_ITEM)
        if got:
            new.append("@" + SECRET_ITEM)
    return new


async def record(players, loser_ids, drops=True):
    """Записывает итог партии. Возвращает ({tg_id: [id достижений]}, {tg_id: вид сундука}, {tg_id: (предмет, дубликат)},
    {tg_id: (старая лига, новая лига, награда или None)} для тех, у кого лига изменилась,
    {tg_id: (эмоция, дубликат)} для выбивших мифическую эмоцию).
    Сундук может выпасть только победителю (drops=False, например при сдаче, отключает выпадение)."""
    if not pool:
        return {}, {}, {}, {}, {}
    out, dropped, mythic, moved, emotes = {}, {}, {}, {}, {}
    try:
        async with pool.acquire() as c, c.transaction():
            for p in players:
                won = p["id"] not in loser_ids
                row = await c.fetchrow("""
                    INSERT INTO players (tg_id, name, rating, coins, wins, losses, win_streak)
                    VALUES ($1,$2,1000+$3,$4,$5,$6,$5)
                    ON CONFLICT (tg_id) DO UPDATE SET
                      name=$2, rating=GREATEST(players.rating+$3,0),
                      coins=players.coins+$4, wins=players.wins+$5,
                      losses=players.losses+$6,
                      win_streak=CASE WHEN $5=1 THEN players.win_streak+1 ELSE 0 END
                    RETURNING rating, league, league_max""",
                    p["id"], p["name"], 25 if won else -15,
                    10 if won else 2, int(won), int(not won))
                cur = row["league"]
                if cur is None:      # старый игрок без сохранённой лиги: считаем по рейтингу до этой партии
                    cur = league_for(row["rating"] - (25 if won else -15))
                lg = update_league(cur, row["rating"])
                await c.execute("UPDATE players SET league=$2 WHERE tg_id=$1", p["id"], lg)
                reward = None
                best = row["league_max"] or 0       # высшая лига, за которую награда уже выдана
                if lg > best:                       # впервые дошли до этой лиги: награда за каждую новую
                    tc = tj = 0
                    chests, items = [], []
                    for i in range(best + 1, lg + 1):
                        rc, rj, rk, ri = LEAGUE_REWARDS[i]
                        tc += rc
                        tj += rj
                        chests += rk
                        if ri:
                            items.append(ri)
                    await c.execute("UPDATE players SET coins=coins+$2, jokers=jokers+$3, league_max=$4 "
                                    "WHERE tg_id=$1", p["id"], tc, tj, lg)
                    for rk in chests:
                        await c.execute(
                            "INSERT INTO player_chests (tg_id, kind, qty) VALUES ($1,$2,1) "
                            "ON CONFLICT (tg_id, kind) DO UPDATE SET qty=player_chests.qty+1", p["id"], rk)
                    for ri in items:
                        await c.execute("INSERT INTO player_items (tg_id, item) VALUES ($1,$2) "
                                        "ON CONFLICT DO NOTHING", p["id"], ri)
                    reward = {"coins": tc, "jokers": tj, "chests": chests, "items": items}
                if lg != cur:
                    moved[p["id"]] = (cur, lg, reward)
                new = await _grant(c, p["id"])
                if new:
                    out[p["id"]] = new
                kind = _roll_drop() if (won and drops) else None
                if kind:
                    await c.execute(
                        "INSERT INTO player_chests (tg_id, kind, qty) VALUES ($1,$2,1) "
                        "ON CONFLICT (tg_id, kind) DO UPDATE SET qty=player_chests.qty+1", p["id"], kind)
                    dropped[p["id"]] = kind
                if won and drops and random.random() * 100 < MYTHIC_CHANCE:
                    item = random.choice(POOL["mythic"])
                    new_item = await c.fetchval(
                        "INSERT INTO player_items (tg_id, item) VALUES ($1,$2) "
                        "ON CONFLICT DO NOTHING RETURNING item", p["id"], item)
                    if not new_item:     # такой уже есть: возвращаем Козырями
                        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1",
                                        p["id"], DUP_REFUND["mythic"])
                    mythic[p["id"]] = (item, not new_item)
                if won and drops and random.random() * 100 < MYTHIC_EMOTE_CHANCE:
                    em = random.choice(EMOTE_POOL["mythic"])
                    new_em = await c.fetchval(
                        "INSERT INTO player_emotes (tg_id, emote) VALUES ($1,$2) "
                        "ON CONFLICT DO NOTHING RETURNING emote", p["id"], em)
                    if not new_em:
                        await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1",
                                        p["id"], EMOTE_DUP_REFUND["mythic"])
                    emotes[p["id"]] = (em, not new_em)
    except Exception as e:
        print("stats error:", e)
        return {}, {}, {}, {}, {}
    return out, dropped, mythic, moved, emotes


async def top(n=10):
    return await pool.fetch(
        "SELECT name, rating, wins, league FROM players ORDER BY rating DESC LIMIT $1", n)


async def ensure(uid, name):
    """Создаёт запись игрока, если её нет. True, если игрок новый."""
    if not pool:
        return False
    try:
        return bool(await pool.fetchval(
            "INSERT INTO players (tg_id, name) VALUES ($1,$2) "
            "ON CONFLICT (tg_id) DO UPDATE SET name=EXCLUDED.name RETURNING (xmax = 0)", uid, name))
    except Exception as e:
        print("ensure error:", e)
        return False


async def bonus_available(uid):
    """Можно ли ещё получить разовую награду (у новых и у всех, кто играл раньше)."""
    r = await pool.fetchrow("SELECT bonus_claimed FROM players WHERE tg_id=$1", uid)
    return not (r and r["bonus_claimed"])


async def claim_bonus(uid, name):
    """Разовая награда START_COINS. Возвращает новый баланс Козырей или None, если уже получена."""
    async with pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO players (tg_id, name) VALUES ($1,$2) ON CONFLICT DO NOTHING", uid, name)
        return await c.fetchval(
            "UPDATE players SET coins=coins+$2, bonus_claimed=TRUE "
            "WHERE tg_id=$1 AND NOT COALESCE(bonus_claimed, FALSE) RETURNING coins", uid, START_COINS)


async def me(uid):
    return await pool.fetchrow("SELECT * FROM players WHERE tg_id=$1", uid)


async def earned(uid):
    rows = await pool.fetch("SELECT ach FROM achievements WHERE tg_id=$1", uid)
    return {r["ach"] for r in rows}


async def spin(uid, name):
    """Колесо фортуны раз в 24 часа.
    Возвращает {"wait": timedelta} или {"label", "coins", "jokers", "ach"}."""
    async with pool.acquire() as c, c.transaction():
        await c.execute(
            "INSERT INTO players (tg_id, name) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            uid, name)
        row = await c.fetchrow(
            "SELECT last_spin FROM players WHERE tg_id=$1 FOR UPDATE", uid)
        now = datetime.now(timezone.utc)
        last = row["last_spin"]
        if last and now - last < SPIN_COOLDOWN:
            return {"wait": SPIN_COOLDOWN - (now - last)}
        label, cur, amount, _ = random.choices(WHEEL, weights=[w[3] for w in WHEEL])[0]
        col = "coins" if cur == "coins" else "jokers"
        await c.execute(
            f"UPDATE players SET {col}={col}+$2, last_spin=$3 WHERE tg_id=$1",
            uid, amount, now)
        extra = {"spin"}
        if cur == "jokers" and amount >= 5:
            extra.add("jackpot")
        ach = await _grant(c, uid, extra)
        bal = await c.fetchrow("SELECT coins, jokers FROM players WHERE tg_id=$1", uid)
        return {"label": label, "coins": bal["coins"], "jokers": bal["jokers"], "ach": ach}


async def add_jokers(uid, name, jokers, stars, charge_id):
    """Начисляет оплаченных Джокеров. Повторная доставка того же платежа
    ничего не начислит (вернёт None). Иначе возвращает (баланс Джокеров, [достижения])."""
    async with pool.acquire() as c, c.transaction():
        got = await c.fetchval(
            "INSERT INTO payments (charge_id, tg_id, jokers, stars) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT DO NOTHING RETURNING charge_id", charge_id, uid, jokers, stars)
        if not got:
            return None
        await c.execute("""
            INSERT INTO players (tg_id, name, jokers) VALUES ($1,$2,$3)
            ON CONFLICT (tg_id) DO UPDATE SET jokers=players.jokers+$3""",
            uid, name, jokers)
        ach = await _grant(c, uid, {"purchase"})
        bal = await c.fetchval("SELECT jokers FROM players WHERE tg_id=$1", uid)
        return bal, ach


async def open_chest(uid, kind, stock=False):
    """Списывает цену (или один сундук со склада, если stock=True) и выдаёт предмет. Списание атомарное: при нехватке валюты
    ничего не происходит. Возвращает {"error"} или {"item", "rarity", "dup", "refund", "coins", "jokers", "ach"}."""
    if kind not in CHESTS:
        return {"error": "nochest"}
    _i, _t, cur, price, odds = CHESTS[kind]
    col = "coins" if cur == "coins" else "jokers"
    async with pool.acquire() as c, c.transaction():
        if stock:
            left = await c.fetchval(
                "UPDATE player_chests SET qty=qty-1 WHERE tg_id=$1 AND kind=$2 AND qty>0 RETURNING qty",
                uid, kind)
            if left is None:
                return {"error": "nostock"}
        else:
            paid = await c.fetchval(
                f"UPDATE players SET {col}={col}-$2 WHERE tg_id=$1 AND {col}>=$2 RETURNING tg_id",
                uid, price)
            if not paid:
                return {"error": "funds"}
        rar = random.choices(list(odds), weights=list(odds.values()))[0]
        item = random.choice(POOL[rar])
        new = await c.fetchval(
            "INSERT INTO player_items (tg_id, item) VALUES ($1,$2) "
            "ON CONFLICT DO NOTHING RETURNING item", uid, item)
        refund = 0
        if not new:
            refund = DUP_REFUND[rar]
            await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, refund)
        extra = {"chest"}
        if rar == "legendary":
            extra.add("legendary")
        if await c.fetchval("SELECT count(*) FROM player_items WHERE tg_id=$1", uid) >= 8:
            extra.add("items8")
        ach = await _grant(c, uid, extra)
        bal = await c.fetchrow("SELECT coins, jokers FROM players WHERE tg_id=$1", uid)
        return {"item": item, "rarity": rar, "dup": not new, "refund": refund,
                "coins": bal["coins"], "jokers": bal["jokers"], "ach": ach}


async def chest_stock(uid):
    """Полученные за победы и ещё не открытые сундуки: {kind: количество}."""
    rows = await pool.fetch("SELECT kind, qty FROM player_chests WHERE tg_id=$1 AND qty>0", uid)
    return {r["kind"]: r["qty"] for r in rows if r["kind"] in CHESTS}


def _roll_drop():
    r = random.random() * 100
    acc = 0.0
    for kind, pct in DROP_CHANCE.items():
        acc += pct
        if r < acc:
            return kind
    return None


async def owned_emotes(uid):
    """Открытые эмоции игрока: базовые есть у всех."""
    have = set(BASE_EMOTES)
    if not pool:
        return have
    try:
        rows = await pool.fetch("SELECT emote FROM player_emotes WHERE tg_id=$1", uid)
    except Exception:
        return have
    return have | {r["emote"] for r in rows if r["emote"] in EMOTES}


async def open_emote_chest(uid):
    """Покупка и открытие сундука эмоций. Возвращает {"error"} или
    {"emote", "rarity", "dup", "refund", "coins", "jokers"}."""
    _i, _t, cur, price, odds = EMOTE_CHEST
    async with pool.acquire() as c, c.transaction():
        paid = await c.fetchval(
            "UPDATE players SET coins=coins-$2 WHERE tg_id=$1 AND coins>=$2 RETURNING tg_id", uid, price)
        if not paid:
            return {"error": "funds"}
        rar = random.choices(list(odds), weights=list(odds.values()))[0]
        em = random.choice(EMOTE_POOL[rar])
        new = await c.fetchval(
            "INSERT INTO player_emotes (tg_id, emote) VALUES ($1,$2) "
            "ON CONFLICT DO NOTHING RETURNING emote", uid, em)
        refund = 0
        if not new:
            refund = EMOTE_DUP_REFUND[rar]
            await c.execute("UPDATE players SET coins=coins+$2 WHERE tg_id=$1", uid, refund)
        bal = await c.fetchrow("SELECT coins, jokers FROM players WHERE tg_id=$1", uid)
        return {"emote": em, "rarity": rar, "dup": not new, "refund": refund,
                "coins": bal["coins"], "jokers": bal["jokers"]}


async def owned_items(uid):
    rows = await pool.fetch("SELECT item FROM player_items WHERE tg_id=$1", uid)
    return {r["item"] for r in rows if r["item"] in ITEMS} | set(DEFAULTS.values())


async def look(uid):
    """Что надето: {"deck": id, "table": id}. Если базы нет, стандартные."""
    out = dict(DEFAULTS)
    if not pool:
        return out
    try:
        r = await pool.fetchrow("SELECT deck, tbl FROM players WHERE tg_id=$1", uid)
    except Exception:
        return out
    if r:
        if r["deck"] in ITEMS:
            out["deck"] = r["deck"]
        if r["tbl"] in ITEMS:
            out["table"] = r["tbl"]
    return out


async def equip(uid, item):
    """Надевает предмет, если он есть у игрока. Возвращает тип предмета или None."""
    if item not in ITEMS or item not in await owned_items(uid):
        return None
    kind = ITEMS[item][0]
    col = "deck" if kind == "deck" else "tbl"
    await pool.execute(f"UPDATE players SET {col}=$2 WHERE tg_id=$1", uid, item)
    return kind


async def skin_list(uid, active):
    """Предметы игрока для выбора на столе. active: {"deck": id, "table": id}."""
    have = await owned_items(uid)
    return [{"id": i, "kind": v[0], "rarity": v[1], "name": v[2], "icon": v[3],
             "on": i in active.values()}
            for i, v in ITEMS.items() if i in have]
