import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery, Update)
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

import stats
from bura import SIZES, Bura
from durak import Durak, GameError

TOKEN = os.environ["BOT_TOKEN"]
APP_NAME = os.getenv("APP_NAME", "durak")  # короткое имя мини-аппа из BotFather
BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL", "")
SECRET = hashlib.sha256(("wh:" + TOKEN).encode()).hexdigest()[:32]
HERE = os.path.dirname(os.path.abspath(__file__))

bot = Bot(TOKEN)
dp = Dispatcher()

DEFAULT_CFG = {
    "durak": {"mode": "podkidnoy", "cheat": False},
    "bura": {"fmt": "1x1", "target": 12},
}
ALLOWED = ["message", "callback_query", "pre_checkout_query"]
SUPPORT = os.getenv("SUPPORT_CONTACT", "администратору бота")
# сколько Джокеров -> цена в Telegram Stars (можно менять)
PACKS = {5: 50, 15: 130, 40: 300}
EMOTES = {"laugh", "angry", "shock", "wink", "gotcha", "crown"}
GAME_ACTIONS = {"attack", "defend", "transfer", "take", "done", "catch",
                "pick", "leader", "continue", "lead", "respond"}
TITLES = {"durak": "Дурак", "bura": "Бураказел"}


class Room:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.conns = {}            # user_id -> set(websocket)
        self.lock = asyncio.Lock()
        self.host = None
        self.seated = []           # [{"id", "name"}], порядок = места за столом
        self.waiting = []
        self.game = None
        self.cfg = {}
        self.phase = "menu"        # menu | play | over
        self.engine = None
        self.result = None
        self.seq = 0
        self.events = []
        self.last_emote = {}
        self.skin = dict(stats.DEFAULTS)   # {"deck", "table"}: выбирает ведущий, видят все
        self.emote_last = None
        self._res_sched = 0
        self._board_sched = 0

    # ------------------------------------------------------------ служебное
    def emit(self, kind, **_):
        self.seq += 1
        self.events.append({"n": self.seq, "k": kind})
        self.events = self.events[-20:]

    def ids(self, lst):
        return [p["id"] for p in lst]

    def capacity(self):
        if self.game == "bura":
            return SIZES[self.cfg["fmt"]]
        return 6

    def minimum(self):
        return self.capacity() if self.game == "bura" else 2

    def ensure_host(self):
        if self.host in self.conns or not self.conns:
            return
        online_seated = [p["id"] for p in self.seated if p["id"] in self.conns]
        self.host = (online_seated or list(self.conns))[0]

    def prune(self):
        self.seated = [p for p in self.seated if p["id"] in self.conns]
        self.waiting = [p for p in self.waiting if p["id"] in self.conns]

    def fit_seats(self):
        cap = self.capacity()
        while len(self.seated) > cap:
            self.waiting.insert(0, self.seated.pop())
        while len(self.seated) < cap and self.waiting:
            self.seated.append(self.waiting.pop(0))

    def rotate(self):
        """Лишние игроки заходят вместо случайных, а заменённые уходят ждать."""
        self.fit_seats()
        if not self.waiting:
            return
        k = min(len(self.waiting), len(self.seated))
        places = random.sample(range(len(self.seated)), k)
        newcomers = random.sample(self.waiting, k)
        for i, nw in zip(places, newcomers):
            old = self.seated[i]
            self.seated[i] = nw
            self.waiting.remove(nw)
            self.waiting.append(old)

    async def set_skin(self, uid, item):
        """Ведущий выбирает колоду или стол из своих предметов. Видят все за столом."""
        self.need_host(uid)
        if not stats.pool:
            raise GameError("Оформление пока недоступно")
        kind = await stats.equip(uid, item)
        if not kind:
            raise GameError("Этого предмета у вас нет")
        self.skin[kind] = item

    def need_host(self, uid):
        if uid != self.host:
            raise GameError("Это может сделать только ведущий")

    async def say(self, text):
        try:
            await bot.send_message(self.chat_id, text)
        except Exception as e:
            print("say error:", e)

    # ------------------------------------------------------------ команды игроков
    def handle(self, uid, name, m):
        t = m.get("t")
        if t == "emote":
            return self.emote(uid, m)
        seated = uid in self.ids(self.seated)
        me = {"id": uid, "name": name}
        if t == "game":
            self.need_host(uid)
            if self.phase == "play":
                raise GameError("Игра уже идёт")
            g = m.get("game")
            if g not in (None, "durak", "bura"):
                raise GameError("Нет такой игры")
            self.game, self.cfg = g, dict(DEFAULT_CFG[g]) if g else {}
            self.phase, self.engine, self.result = "menu", None, None
            self.fit_seats()
        elif t == "cfg":
            self.need_host(uid)
            if self.phase == "play" or not self.game:
                raise GameError("Сейчас нельзя менять настройки")
            k, v = m.get("k"), m.get("v")
            if self.game == "durak" and k == "mode" and v in ("podkidnoy", "perevodnoy"):
                self.cfg["mode"] = v
            elif self.game == "durak" and k == "cheat" and isinstance(v, bool):
                self.cfg["cheat"] = v
            elif self.game == "bura" and k == "fmt" and v in SIZES:
                self.cfg["fmt"] = v
            elif self.game == "bura" and k == "target" and v in (6, 12):
                self.cfg["target"] = v
            else:
                raise GameError("Неверная настройка")
            self.fit_seats()
        elif t == "join":
            if seated or uid in self.ids(self.waiting):
                return
            if self.phase == "play":
                self.waiting.append(me)
            else:
                self.prune()
                (self.seated if len(self.seated) < self.capacity() else self.waiting).append(me)
        elif t == "leave":
            if self.phase == "play" and seated:
                raise GameError("Во время игры можно только сдаться")
            self.seated = [p for p in self.seated if p["id"] != uid]
            self.waiting = [p for p in self.waiting if p["id"] != uid]
            if self.phase != "play":
                self.fit_seats()
        elif t == "start":
            self.start(uid)
        elif t == "menu":
            self.need_host(uid)
            if self.phase == "play":
                raise GameError("Игра уже идёт")
            self.game, self.cfg, self.phase, self.engine, self.result = None, {}, "menu", None, None
        elif t == "surrender":
            if self.phase != "play" or not seated:
                raise GameError("Сейчас нельзя сдаться")
            self.engine.surrender(uid)
        elif t in GAME_ACTIONS and self.phase == "play" and self.engine:
            self.engine.act(uid, m)
        else:
            raise GameError("Сейчас нельзя")

    def start(self, uid):
        if not self.game:
            raise GameError("Сначала выберите игру")
        if self.phase == "menu":
            self.need_host(uid)
        elif self.phase == "over":
            if uid not in self.ids(self.seated):
                raise GameError("Начать может только тот, кто за столом")
        else:
            raise GameError("Игра уже идёт")
        self.prune()
        if self.phase == "over":
            self.rotate()      # после партии: ожидающие случайно заходят вместо играющих
        else:
            self.fit_seats()
        if len(self.seated) < self.minimum():
            raise GameError(f"Нужно игроков: {self.minimum()}")
        players = [dict(p) for p in self.seated]
        emit = self.emit
        if self.game == "durak":
            self.engine = Durak(players, self.cfg["mode"] == "perevodnoy", self.cfg["cheat"], emit)
        else:
            self.engine = Bura(players, self.cfg["fmt"], self.cfg["target"], emit)
        self.phase, self.result = "play", None
        self._res_sched = self._board_sched = 0
        self.emit("start")
        if self.game == "durak":
            extra = " (" + ("переводной" if self.cfg["mode"] == "perevodnoy" else "подкидной") + (", шулерство" if self.cfg["cheat"] else "") + ")"
        else:
            extra = f" (до {self.cfg['target']} штрафных баллов)"
        names = ", ".join(p["name"] for p in players)
        asyncio.create_task(self.say(f"🃏 Началась игра: {TITLES[self.game]}{extra}\nИграют: {names}"))

    def emote(self, uid, m):
        k = m.get("k")
        if k not in EMOTES:
            return
        now = time.time()
        if now - self.last_emote.get(uid, 0) < 1.5:
            raise GameError("Не так быстро")
        self.last_emote[uid] = now
        self.seq += 1
        self.emote_last = {"id": uid, "k": k, "n": self.seq}

    # ------------------------------------------------------------ ход игры по таймерам
    def kick(self):
        eng = self.engine
        if not eng or self.phase != "play":
            return
        if eng.over:
            return self.finish_game()
        if eng.res and self._res_sched != eng.res_id:
            self._res_sched = eng.res_id
            asyncio.create_task(self._run_res(eng, eng.res_id, eng.res.get("delay", 2.2)))
        if getattr(eng, "phase", None) == "board" and self._board_sched != eng.board_id:
            self._board_sched = eng.board_id
            asyncio.create_task(self._run_board(eng, eng.board_id))

    async def _run_res(self, eng, rid, delay):
        await asyncio.sleep(delay)
        async with self.lock:
            if self.engine is eng and eng.res and eng.res_id == rid:
                eng.resolve()
                self.kick()
                await self.broadcast()

    async def _run_board(self, eng, bid):
        await asyncio.sleep(20)
        async with self.lock:
            if self.engine is eng and not eng.over and eng.phase == "board" and eng.board_id == bid:
                eng.continue_()
                self.kick()
                await self.broadcast()

    def finish_game(self):
        eng = self.engine
        names = {p["id"]: p["name"] for p in eng.players}
        if eng.kind == "durak":
            loser_ids = [eng.loser] if eng.loser is not None else []
        else:
            loser_ids = list(eng.loser_ids)
        if eng.surrendered:
            who = names.get(loser_ids[0], "Игрок") if loser_ids else "Игрок"
            text = f"{who} сдался и остался в дураках"
        elif not loser_ids:
            text = "Ничья, все избавились от карт"
        elif eng.kind == "durak":
            text = f"Дурак: {names[loser_ids[0]]}"
        elif len(loser_ids) > 1:
            text = "Проиграла команда: " + " и ".join(names[i] for i in loser_ids)
        else:
            text = f"{names[loser_ids[0]]} проиграл"
        self.result = {"text": text, "loser_ids": loser_ids, "surrendered": bool(eng.surrendered)}
        self.phase = "over"
        if loser_ids:
            asyncio.create_task(award(self, eng.players, loser_ids, bool(eng.surrendered)))
        self.emit("over")
        asyncio.create_task(self.say(f"🏁 {text}"))

    # ------------------------------------------------------------ что видит игрок
    def view(self, uid):
        v = {
            "me": uid, "phase": self.phase, "game": self.game, "cfg": self.cfg, "host": self.host,
            "seats": [{"id": p["id"], "name": p["name"], "on": p["id"] in self.conns} for p in self.seated],
            "deck": self.skin["deck"], "table": self.skin["table"],
            "wait": [{"id": p["id"], "name": p["name"]} for p in self.waiting],
            "cap": self.capacity(), "min": self.minimum(),
            "seated": uid in self.ids(self.seated), "queued": uid in self.ids(self.waiting),
            "ev": self.events[-12:], "seq": self.seq, "emote": self.emote_last,
            "result": self.result,
        }
        if self.engine:
            v["g"] = self.engine.view(uid)
        return v

    async def broadcast(self):
        for uid, sockets in list(self.conns.items()):
            payload = self.view(uid)
            for w in list(sockets):
                try:
                    await w.send_json(payload)
                except Exception:
                    sockets.discard(w)


ROOMS = {}


async def award(room, players, loser_ids, surrendered=False):
    """Записывает итог партии, объявляет достижения и выпавшие победителям сундуки в чате группы."""
    got, dropped, mythic = await stats.record(players, loser_ids, drops=not surrendered)
    names = {p["id"]: p["name"] for p in players}
    lines = [f"🏅 {names[u]}: {stats.describe(a)}" for u, ids in got.items() for a in ids]
    for u, kind in dropped.items():
        icon, title = stats.CHESTS[kind][0], stats.CHESTS[kind][1]
        lines.append(f"🎁 {names[u]} получил за победу: {icon} {title}! Открыть: /chests в личке с ботом")
    for u, (item, dup) in mythic.items():
        tail = f" Такой уже был, +{stats.DUP_REFUND['mythic']} ♠️" if dup else " Надеть: /collection в личке с ботом"
        lines.append(f"🟣 {names[u]} выбил МИФИЧЕСКИЙ предмет за победу: {stats.item_title(item)}! 🎉{tail}")
    if lines:
        await room.say("\n".join(lines))

# ------------------------------------------------------- проверка доступа


def check_init_data(init_data):
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", None)
    if not got:
        raise ValueError("no hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        raise ValueError("bad hash")
    if time.time() - int(data.get("auth_date", "0")) > 2 * 86400:
        raise ValueError("expired")
    return data


async def is_member(chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if m.status in ("creator", "administrator", "member"):
        return True
    return m.status == "restricted" and bool(getattr(m, "is_member", False))


# ------------------------------------------------------------------ бот


@dp.message(Command("start"))
async def cmd_start(m: Message):
    await stats.ensure(m.from_user.id, (m.from_user.first_name or str(m.from_user.id))[:20]) if stats.pool else None
    await m.answer(
        "Привет! Я игровой бот. Добавьте меня в группу и напишите там /play, "
        "чтобы открыть стол: Дурак или Бураказел.\n\n"
        "/top — рейтинг, /me — профиль, /wheel — колесо фортуны раз в сутки, /shop — купить Джокеров, /achievements — достижения, /chests — сундуки, /collection — колоды и столы.",
        reply_markup=await _bonus_kb(m.from_user.id),
    )


async def _bonus_kb(uid):
    """Кнопка разовой награды: есть, пока человек её не получил."""
    if not stats.pool:
        return None
    try:
        if not await stats.bonus_available(uid):
            return None
    except Exception:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎁 Получить вознаграждение", callback_data="bonus")]])


@dp.message(Command("bonus"))
async def cmd_bonus(m: Message):
    kb = await _bonus_kb(m.from_user.id)
    if kb:
        return await m.answer(f"🎁 Разовая награда: {stats.START_COINS} ♠️ Козырей.", reply_markup=kb)
    await m.answer("Вы уже получили разовую награду.")


@dp.callback_query(F.data == "bonus")
async def cb_bonus(q: CallbackQuery):
    if not stats.pool:
        return await q.answer("Пока недоступно", show_alert=True)
    name = (q.from_user.first_name or str(q.from_user.id))[:20]
    bal = await stats.claim_bonus(q.from_user.id, name)
    try:
        await q.message.edit_reply_markup(reply_markup=None)   # кнопка пропадает
    except Exception:
        pass
    if bal is None:
        return await q.answer("Вы уже получили эту награду", show_alert=True)
    await q.answer()
    await q.message.answer(f"🎁 +{stats.START_COINS} ♠️ Козырей!\n♠️ Козыри: {bal}\n\nПотратить: /chests")


@dp.message(Command("play"))
async def cmd_play(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        await m.answer("Эта команда работает в группе. Добавьте меня в группу и напишите /play там.")
        return
    me = await bot.get_me()
    url = f"https://t.me/{me.username}/{APP_NAME}?startapp={m.chat.id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🃏 Играть", url=url)]])
    await m.answer("Стол готов. Играть могут только участники этой группы.", reply_markup=kb)


@dp.message(Command("top"))
async def cmd_top(m: Message):
    if not stats.pool:
        return await m.answer("Рейтинг пока недоступен")
    rows = await stats.top()
    if not rows:
        return await m.answer("Пока никто не сыграл")
    await m.answer("🏆 Топ игроков:\n" + "\n".join(
        f"{i}. {r['name']}: {r['rating']} ({r['wins']} побед)"
        for i, r in enumerate(rows, 1)))


@dp.message(Command("me"))
async def cmd_me(m: Message):
    r = await stats.me(m.from_user.id) if stats.pool else None
    kb = await _bonus_kb(m.from_user.id)
    if not r:
        return await m.answer("Вы ещё не играли", reply_markup=kb)
    await m.answer(f"Рейтинг: {r['rating']}\n♠️ Козыри: {r['coins']}\n🃏 Джокеры: {r['jokers']}\n"
                   f"Победы: {r['wins']}, поражения: {r['losses']}", reply_markup=kb)


@dp.message(Command("achievements"))
async def cmd_ach(m: Message):
    if not stats.pool:
        return await m.answer("Достижения пока недоступны")
    have = await stats.earned(m.from_user.id)
    lines = [f"🏅 Достижения: {len(have)}/{len(stats.ACH)}\n"]
    for aid, (icon, title, desc, coins, jokers, _c) in stats.ACH.items():
        if aid in have:
            lines.append(f"✅ {icon} {title}")
        else:
            lines.append(f"🔒 {title}: {desc} ({stats._reward(coins, jokers)})")
    await m.answer("\n".join(lines))


@dp.message(Command("wheel"))
async def cmd_wheel(m: Message):
    if not stats.pool:
        return await m.answer("Колесо пока недоступно")
    uid = m.from_user.id
    name = (m.from_user.first_name or m.from_user.username or str(uid))[:20]
    res = await stats.spin(uid, name)
    if "wait" in res:
        mins = int(res["wait"].total_seconds() // 60)
        return await m.answer(f"⏳ Колесо уже крутили сегодня. Следующая попытка через {mins // 60} ч {mins % 60} мин")
    msg = await m.answer("🎡 Колесо крутится...")
    await asyncio.sleep(2)
    text = (f"🎡 Выпало: {res['label']}!\n\n"
            f"♠️ Козыри: {res['coins']}\n🃏 Джокеры: {res['jokers']}")
    for a in res["ach"]:
        text += f"\n\n🏅 Достижение: {stats.describe(a)}"
    try:
        await msg.edit_text(text)
    except Exception:
        await m.answer(text)


@dp.message(Command("shop"))
async def cmd_shop(m: Message):
    if m.chat.type != "private":
        return await m.answer("Магазин работает в личных сообщениях с ботом.")
    if not stats.pool:
        return await m.answer("Магазин пока недоступен")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🃏 {n} Джокеров — {p} ⭐", callback_data=f"buy:{n}")]
        for n, p in PACKS.items()])
    await m.answer("🃏 Джокеры — донатная валюта. Оплата в Telegram Stars:", reply_markup=kb)


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(q: CallbackQuery):
    try:
        n = int(q.data.split(":")[1])
    except ValueError:
        return await q.answer()
    if n not in PACKS or not stats.pool:
        return await q.answer("Недоступно", show_alert=True)
    await q.answer()
    await bot.send_invoice(
        chat_id=q.from_user.id,
        title=f"{n} Джокеров",
        description=f"Донатная валюта 🃏 Джокеры: {n} шт.",
        payload=f"jokers:{n}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{n} Джокеров", amount=PACKS[n])],
    )


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try:
        n = int(q.invoice_payload.split(":")[1])
        ok = stats.pool is not None and q.currency == "XTR" and PACKS.get(n) == q.total_amount
    except Exception:
        ok = False
    await q.answer(ok=ok, error_message=None if ok else "Покупка сейчас недоступна, попробуйте позже")


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    n = int(sp.invoice_payload.split(":")[1])
    name = (m.from_user.first_name or m.from_user.username or str(m.from_user.id))[:20]
    bal = await stats.add_jokers(m.from_user.id, name, n, sp.total_amount,
                                 sp.telegram_payment_charge_id)
    if bal is None:
        return
    bal, ach = bal
    text = f"✅ Спасибо! Начислено {n} 🃏 Джокеров. Теперь у вас: {bal}"
    for a in ach:
        text += f"\n\n🏅 Достижение: {stats.describe(a)}"
    await m.answer(text)


# ---------------------------------------------------------------- сундуки


def _odds(odds):
    return " / ".join(f"{stats.RARITY[r][0]} {p}%" for r, p in odds.items() if p)


@dp.message(Command("chests"))
async def cmd_chests(m: Message):
    if m.chat.type != "private":
        return await m.answer("Сундуки открываются в личных сообщениях с ботом.")
    if not stats.pool:
        return await m.answer("Сундуки пока недоступны")
    await stats.ensure(m.from_user.id, (m.from_user.first_name or str(m.from_user.id))[:20])
    rows, lines = [], ["🎁 Сундуки. В них новые колоды и столы (только внешний вид).\n"]
    stock = await stats.chest_stock(m.from_user.id)
    if stock:
        lines.append("Ваши сундуки за победы: " + ", ".join(
            f"{stats.CHESTS[k][0]} {stats.CHESTS[k][1]} x{n}" for k, n in stock.items()) + "\n")
        for k, n in stock.items():
            rows.append([InlineKeyboardButton(text=f"{stats.CHESTS[k][0]} Открыть бесплатно (x{n})", callback_data=f"open:{k}")])
    lines.append("За победу в партии можно получить сундук (шанс небольшой: " + ", ".join(
        f"{stats.CHESTS[k][0]} {p:g}%" for k, p in stats.DROP_CHANCE.items()) + ").\n")
    for cid, (icon, title, cur, price, odds) in stats.CHESTS.items():
        sym = "♠️" if cur == "coins" else "🃏"
        lines.append(f"{icon} {title}: {price} {sym}\nШансы: {_odds(odds)}\n")
        rows.append([InlineKeyboardButton(text=f"{icon} {title}: {price} {sym}", callback_data=f"chest:{cid}")])
    lines.append("Если предмет уже есть, вернём Козыри: " + ", ".join(
        f"{stats.RARITY[r][0]} {v}" for r, v in stats.DUP_REFUND.items()))
    await m.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("chest:"))
async def cb_chest(q: CallbackQuery):
    await _open_chest_cb(q, False)


@dp.callback_query(F.data.startswith("open:"))
async def cb_open_stock(q: CallbackQuery):
    await _open_chest_cb(q, True)


async def _open_chest_cb(q: CallbackQuery, stock: bool):
    if not stats.pool:
        return await q.answer("Недоступно", show_alert=True)
    kind = q.data.split(":", 1)[1]
    uid = q.from_user.id
    res = await stats.open_chest(uid, kind, stock=stock)
    if res.get("error") == "funds":
        return await q.answer("Не хватает валюты. Колесо, игры и /shop помогут", show_alert=True)
    if res.get("error") == "nostock":
        return await q.answer("Таких сундуков больше нет. Выигрывайте партии!", show_alert=True)
    if "error" in res:
        return await q.answer("Недоступно", show_alert=True)
    await q.answer()
    msg = await q.message.answer("🎁 Открываем сундук...")
    await asyncio.sleep(2)
    text = f"🎁 Вам выпало:\n{stats.item_title(res['item'])}"
    if res["dup"]:
        text += f"\n\n♻️ Уже есть. Возвращаем +{res['refund']} ♠️"
    else:
        text += "\n\nНадеть: /collection"
    text += f"\n\n♠️ Козыри: {res['coins']}\n🃏 Джокеры: {res['jokers']}"
    for a in res["ach"]:
        text += f"\n\n🏅 Достижение: {stats.describe(a)}"
    try:
        await msg.edit_text(text)
    except Exception:
        await q.message.answer(text)


async def _collection(uid):
    have = await stats.owned_items(uid)
    cur = await stats.look(uid)
    worn = set(cur.values())
    droppable = [i for r in stats.POOL.values() for i in r]
    lines = [f"🗂️ Коллекция: {len(have & set(droppable))}/{len(droppable)}\n"]
    for r, (ic, name) in stats.RARITY.items():
        got = [i for i in stats.POOL[r] if i in have]
        lines.append(f"{ic} {name}: {len(got)}/{len(stats.POOL[r])}")
        for i in got:
            lines.append(f"   {'✅' if i in worn else '•'} {stats.item_title(i)}")
    rows = [[InlineKeyboardButton(text=("✅ " if i in worn else "") + stats.ITEMS[i][3] + " " + stats.ITEMS[i][2],
                                  callback_data=f"eq:{i}")]
            for i in stats.ITEMS if i in have]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("collection"))
async def cmd_collection(m: Message):
    if m.chat.type != "private":
        return await m.answer("Коллекция открывается в личных сообщениях с ботом.")
    if not stats.pool:
        return await m.answer("Коллекция пока недоступна")
    text, kb = await _collection(m.from_user.id)
    await m.answer(text + "\n\nНажмите на предмет, чтобы надеть его.", reply_markup=kb)


@dp.callback_query(F.data.startswith("eq:"))
async def cb_equip(q: CallbackQuery):
    if not stats.pool:
        return await q.answer("Недоступно", show_alert=True)
    if not await stats.equip(q.from_user.id, q.data.split(":", 1)[1]):
        return await q.answer("Этого предмета у вас нет", show_alert=True)
    await q.answer("Надето. Будет на столе, который вы откроете первым. Сменить можно и в самой игре")
    text, kb = await _collection(q.from_user.id)
    try:
        await q.message.edit_text(text + "\n\nНажмите на предмет, чтобы надеть его.", reply_markup=kb)
    except Exception:
        pass


@dp.message(Command("paysupport"))
async def cmd_paysupport(m: Message):
    await m.answer(f"Вопросы по оплате и возвратам: напишите {SUPPORT}.")


# ---------------------------------------------------------------- сервер


@asynccontextmanager
async def lifespan(app):
    await stats.init()
    if BASE_URL:
        url = f"{BASE_URL}/webhook"
        try:
            info = await bot.get_webhook_info()
            if info.url != url or set(info.allowed_updates or []) != set(ALLOWED):
                await bot.set_webhook(url, secret_token=SECRET, allowed_updates=ALLOWED)
        except Exception as e:
            print("webhook error:", e)
    yield
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/snd/{name}")
async def snd(name: str):
    path = os.path.join(HERE, name)
    if not re.fullmatch(r"[a-z]+\.mp3", name) or not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        raise HTTPException(status_code=403)
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    room, uid = None, None
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=20)
        try:
            data = check_init_data(first.get("initData", ""))
            user = json.loads(data["user"])
            chat_id = int(data["start_param"])
        except Exception:
            await ws.send_json({"fatal": "Откройте игру кнопкой в группе (команда /play)."})
            await ws.close()
            return
        uid = user["id"]
        if not await is_member(chat_id, uid):
            await ws.send_json({"fatal": "Играть могут только участники этой группы."})
            await ws.close()
            return
        room = ROOMS.setdefault(chat_id, Room(chat_id))
        name = (user.get("first_name") or user.get("username") or str(uid))[:20]
        await stats.ensure(uid, name)
        look = await stats.look(uid)
        async with room.lock:
            was_empty = not room.conns
            room.conns.setdefault(uid, set()).add(ws)
            if room.host is None:
                room.host = uid
            room.ensure_host()
            if was_empty:      # стол открыли заново: оформление берём у первого вошедшего
                room.skin = dict(look)
            await room.broadcast()
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict) or msg.get("t") == "ping":
                continue
            if msg.get("t") == "skins":      # список своих предметов для выбора (только ведущий)
                async with room.lock:
                    is_host = uid == room.host
                    active = dict(room.skin)
                if is_host and stats.pool:
                    await ws.send_json({"skins": await stats.skin_list(uid, active)})
                else:
                    await ws.send_json({"err": "Это может сделать только ведущий"})
                continue
            if msg.get("t") == "skin":
                async with room.lock:
                    try:
                        await room.set_skin(uid, msg.get("item"))
                    except GameError as e:
                        await ws.send_json({"err": str(e)})
                    except Exception as e:
                        print("skin error:", repr(e))
                        await ws.send_json({"err": "Не получилось сменить оформление"})
                    await room.broadcast()
                continue
            async with room.lock:
                try:
                    room.handle(uid, name, msg)
                except GameError as e:
                    await ws.send_json({"err": str(e)})
                except Exception as e:
                    print("handle error:", repr(e))
                    await ws.send_json({"err": "Что-то пошло не так, попробуйте ещё раз"})
                room.kick()
                await room.broadcast()
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as e:
        print("ws error:", repr(e))
    finally:
        if room is not None and uid is not None:
            async with room.lock:
                sockets = room.conns.get(uid)
                if sockets:
                    sockets.discard(ws)
                    if not sockets:
                        del room.conns[uid]
                room.ensure_host()
                await room.broadcast()
