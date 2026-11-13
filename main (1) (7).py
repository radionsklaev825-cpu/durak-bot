import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape as esc
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery, Update)
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

import stats
from bot_ai import BuraAuto, DurakBot
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
EMOTES = set(stats.EMOTES)
GAME_ACTIONS = {"attack", "defend", "transfer", "take", "done", "catch",
                "pick", "leader", "continue", "lead", "respond"}
TITLES = {"durak": "Дурак", "bura": "Бураказел"}
BOT_ID = -7777             # служебный id бота (в базу не попадает)
BOT_NAME = "Бот Хитрец"
# Засчитывать ли рейтинг и награды за партии с ботом (пока игроков мало: да).
# Чтобы выключить: переменная окружения BOT_REWARDS=0 в Render.
BOT_REWARDS = (os.getenv("BOT_REWARDS") or "1") != "0"
# Таймер хода: если игрок молчит столько секунд, за него ходит бот (вернуться можно любым своим ходом).
TURN_SECONDS = int(os.getenv("TURN_SECONDS") or 60)
OFFLINE_SECONDS = int(os.getenv("OFFLINE_SECONDS") or 12)   # то же, если игрок вышел из игры
TIMER_TICK = 1.0                                              # как часто проверяем таймер, секунд


class Room:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.public = isinstance(chat_id, str)   # общий стол «со всем миром»
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
        self.owned_emotes = {}              # {uid: набор открытых эмоций}
        self.solo = False                   # режим «против бота»
        self.bot_ids = set()
        self.brains = {}                    # {id бота: DurakBot}
        self._bot_pending = False
        self.auto = set()                   # игроки, за которых сейчас ходит бот (вышло время)
        self.autobrain = {}
        self._sig = None                    # «отпечаток» состояния партии для таймера
        self._since = time.time()           # когда состояние изменилось в последний раз
        self._actors = []                   # кто сейчас должен ходить
        self._timer_task = None
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
        if self.solo:
            return 2
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
        self.seated = [p for p in self.seated if p["id"] in self.conns or p["id"] in self.bot_ids]
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
        if self.public:
            for p in list(self.seated):
                if p["id"] in self.bot_ids:
                    continue
                try:
                    await bot.send_message(p["id"], text)
                except Exception:
                    pass
            return
        try:
            await bot.send_message(self.chat_id, text)
        except Exception as e:
            print("say error:", e)

    # ------------------------------------------------------------ команды игроков
    def handle(self, uid, name, m):
        t = m.get("t")
        if uid in self.auto and (t in GAME_ACTIONS or t == "surrender"):
            self.auto.discard(uid)          # игрок вернулся: бот больше не ходит за него
            self.autobrain.pop(uid, None)
        if t == "emote":
            return self.emote(uid, m)
        seated = uid in self.ids(self.seated)
        me = {"id": uid, "name": name}
        if self.solo and t in ("game", "menu"):
            raise GameError("В режиме с ботом доступен только Дурак")
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
            elif self.game == "durak" and k == "cheat" and isinstance(v, bool) and not (self.solo and v):
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
        for b in self.brains.values():
            b.reset()
        self.auto.clear()
        self.autobrain.clear()
        self._sig, self._actors, self._since = None, [], time.time()
        if self._timer_task:
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._timer_loop(self.engine))
        self.phase, self.result = "play", None
        self._res_sched = self._board_sched = 0
        self.emit("start")
        if self.game == "durak":
            extra = " (" + ("переводной" if self.cfg["mode"] == "perevodnoy" else "подкидной") + (", шулерство" if self.cfg["cheat"] else "") + ")"
        else:
            extra = f" (до {self.cfg['target']} штрафных баллов)"
        names = ", ".join(p["name"] for p in players)
        if not self.solo:
            asyncio.create_task(self.say(f"🃏 Началась игра: {TITLES[self.game]}{extra}\nИграют: {names}"))

    def emote(self, uid, m):
        k = m.get("k")
        if k not in EMOTES:
            return
        if k not in self.owned_emotes.get(uid, set(stats.BASE_EMOTES)):
            raise GameError("Эта эмоция вам ещё не открыта")
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
        sig = self._state_sig(eng)
        if sig != self._sig:                # что-то изменилось: таймер начинается заново
            self._sig, self._since = sig, time.time()
            self._actors = [] if eng.res else self._compute_actors(eng)
        if eng.res and self._res_sched != eng.res_id:
            self._res_sched = eng.res_id
            asyncio.create_task(self._run_res(eng, eng.res_id, eng.res.get("delay", 2.2)))
        if getattr(eng, "phase", None) == "board" and self._board_sched != eng.board_id:
            self._board_sched = eng.board_id
            asyncio.create_task(self._run_board(eng, eng.board_id))
        if self.brains or self.auto:
            self._bot_check(eng)

    # ------------------------------------------------------------ таймер хода и автоход
    def _new_brain(self):
        return BuraAuto() if self.game == "bura" else DurakBot()

    def _ai_players(self):
        """Кто сейчас играет «автоматически»: боты-соперники и игроки, у которых вышло время."""
        d = dict(self.brains)
        for uid in self.auto:
            if uid not in d:
                d[uid] = self.autobrain.setdefault(uid, self._new_brain())
        return d

    @staticmethod
    def _state_sig(eng):
        try:
            return hash(json.dumps(eng.view(-1), sort_keys=True, default=str))
        except Exception:
            return None

    def _compute_actors(self, eng):
        """Кто из людей за столом сейчас должен сделать ход."""
        scratch = self._new_brain()
        out = []
        for p in self.seated:
            uid = p["id"]
            if uid in self.bot_ids:
                continue
            try:
                if scratch.decide(eng.view(uid), uid):
                    out.append(uid)
            except Exception as e:
                print("actors error:", repr(e))
        return out

    def _timer_payload(self):
        left = self._timer_left()
        if left is None:
            return None
        return {"uids": [u for u in self._actors if u not in self.auto], "left": left}

    def _timer_left(self):
        """Сколько секунд осталось тому, кто должен ходить (None = таймер не идёт)."""
        eng = self.engine
        if self.phase != "play" or not eng or eng.over or eng.res or not self._actors:
            return None
        if all(u in self.auto for u in self._actors):
            return None
        limit = OFFLINE_SECONDS if any(u not in self.conns for u in self._actors) else TURN_SECONDS
        return max(0, int(limit - (time.time() - self._since)))

    async def _timer_loop(self, eng):
        try:
            while True:
                await asyncio.sleep(TIMER_TICK)
                async with self.lock:
                    if self.engine is not eng or self.phase != "play" or eng.over:
                        return
                    if not self.conns or eng.res or not self._actors:
                        continue
                    fresh = [u for u in self._actors if u not in self.auto]
                    if not fresh:
                        continue
                    limit = OFFLINE_SECONDS if any(u not in self.conns for u in fresh) else TURN_SECONDS
                    if time.time() - self._since < limit:
                        continue
                    names = {p["id"]: p["name"] for p in self.seated}
                    self.auto.update(fresh)
                    eng.log = "⏱ Время вышло: за " + ", ".join(names.get(u, "игрока") for u in fresh) + " играет бот"
                    self._since = time.time()
                    self.kick()
                    await self.broadcast()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print("timer error:", repr(e))

    # ------------------------------------------------------------ бот-соперник
    def _bot_check(self, eng):
        """Если боту есть что делать, планируем его ход с небольшой «задумчивостью»."""
        for bid, brain in self._ai_players().items():
            try:
                v = eng.view(bid)
                brain.observe(v, bid)
                want = bool(brain.decide(v, bid))
            except Exception as e:
                print("bot check error:", repr(e))
                continue
            if want and not self._bot_pending and self.conns:
                self._bot_pending = True
                asyncio.create_task(self._bot_tick(eng))

    async def _bot_tick(self, eng):
        try:
            await asyncio.sleep(random.uniform(0.9, 1.8))
            async with self.lock:
                self._bot_pending = False
                if self.engine is not eng or self.phase != "play" or eng.over or not self.conns:
                    return
                for bid, brain in self._ai_players().items():
                    v = eng.view(bid)
                    cands = brain.decide(v, bid)
                    done = False
                    for msg in cands:
                        try:
                            eng.act(bid, msg)
                            done = True
                            break
                        except GameError:
                            continue
                    if cands and not done:      # движок отверг все варианты: не зацикливаемся
                        brain.blocked = (tuple(map(tuple, v.get("table") or [])),
                                         tuple(sorted(v.get("hand") or [])), bool(v.get("taking")))
                self.kick()
                await self.broadcast()
        except Exception as e:
            print("bot tick error:", repr(e))
            self._bot_pending = False

    def _bot_react(self, loser_ids):
        """Бот иногда радуется победе или злится."""
        if not loser_ids:
            return
        for bid in self.bot_ids:
            win = bid not in loser_ids
            try:
                self.emote(bid, {"k": random.choice(["crown", "laugh", "wink"] if win else ["shock", "angry"])})
            except GameError:
                pass

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
        self.auto.clear()
        self.autobrain.clear()
        self._actors = []
        if self.solo:
            self._bot_react(loser_ids)
        if loser_ids and (not self.solo or BOT_REWARDS):
            asyncio.create_task(award(self, eng.players, loser_ids, bool(eng.surrendered)))
        self.emit("over")
        asyncio.create_task(self.say(f"🏁 {text}"))

    # ------------------------------------------------------------ что видит игрок
    def view(self, uid):
        v = {
            "me": uid, "phase": self.phase, "game": self.game, "cfg": self.cfg, "host": self.host,
            "seats": [{"id": p["id"], "name": p["name"], "on": p["id"] in self.conns or p["id"] in self.bot_ids} for p in self.seated],
            "deck": self.skin["deck"], "table": self.skin["table"],
            "wait": [{"id": p["id"], "name": p["name"]} for p in self.waiting],
            "cap": self.capacity(), "min": self.minimum(),
            "seated": uid in self.ids(self.seated), "queued": uid in self.ids(self.waiting),
            "ev": self.events[-12:], "seq": self.seq, "emote": self.emote_last,
            "result": self.result, "solo": self.solo,
            "timer": self._timer_payload(), "auto": sorted(self.auto),
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
GLOBAL = "global"          # startapp=global -> общий стол
GLOBAL_CAP = 6             # сколько человек максимум в одном общем столе
_global_n = 0


def pick_global_room(uid):
    """Возвращает общий стол: тот, где игрок уже сидит, иначе самый заполненный свободный, иначе новый."""
    global _global_n
    for r in ROOMS.values():
        if r.public and not r.solo and (uid in r.conns or uid in r.ids(r.seated) or uid in r.ids(r.waiting)):
            return r
    best = None
    for r in ROOMS.values():
        if r.public and not r.solo and r.phase != "play" and len(r.conns) < GLOBAL_CAP:
            if best is None or len(r.conns) > len(best.conns):
                best = r
    if best:
        return best
    _global_n += 1
    r = Room(f"g{_global_n}")
    ROOMS[r.chat_id] = r
    return r


def solo_room(uid):
    """Личный стол против бота: у каждого игрока свой."""
    key = f"bot:{uid}"
    r = ROOMS.get(key)
    if r is None:
        r = Room(key)
        r.solo = True
        r.game, r.cfg = "durak", dict(DEFAULT_CFG["durak"])
        r.bot_ids = {BOT_ID}
        r.brains = {BOT_ID: DurakBot()}
        r.seated.append({"id": BOT_ID, "name": BOT_NAME})
        ROOMS[key] = r
    return r


async def drop_later(room, delay=600):
    """Пустой общий стол удаляется через 10 минут (на случай, если игроки просто переподключаются)."""
    await asyncio.sleep(delay)
    if room.public and not room.conns and ROOMS.get(room.chat_id) is room:
        del ROOMS[room.chat_id]


async def award(room, players, loser_ids, surrendered=False):
    """Записывает итог партии, объявляет достижения и выпавшие победителям сундуки в чате группы."""
    players = [p for p in players if p["id"] != BOT_ID]     # бот в статистику и рейтинг не попадает
    if not players:
        return
    got, dropped, mythic, moved, emotes = await stats.record(players, loser_ids, drops=not surrendered)
    names = {p["id"]: p["name"] for p in players}
    lines = [f"🏅 {names[u]}: {stats.describe(a)}" for u, ids in got.items() for a in ids]
    for u, (old_l, new_l, rw) in moved.items():
        arrow = "⬆️ Новая лига" if new_l > old_l else "⬇️ Переход в лигу"
        line = f"{arrow}: {names[u]} → {stats.league_title(new_l)}"
        if rw:
            parts = []
            if rw["coins"]:
                parts.append(f"+{rw['coins']} ♠️")
            if rw["jokers"]:
                parts.append(f"+{rw['jokers']} 🃏")
            parts += [f"{stats.CHESTS[k][0]} {stats.CHESTS[k][1]}" for k in rw["chests"]]
            parts += [stats.item_title(i) for i in rw.get("items", [])]
            line += "\n🎁 Награда за лигу: " + " ".join(parts)
            if rw["chests"] or rw.get("items"):
                line += "\nСундуки и предметы: /chests и /collection в личке с ботом"
        lines.append(line)
    for u, kind in dropped.items():
        icon, title = stats.CHESTS[kind][0], stats.CHESTS[kind][1]
        lines.append(f"🎁 {names[u]} получил за победу: {icon} {title}! Открыть: /chests в личке с ботом")
    for u, (item, dup) in mythic.items():
        tail = f" Такой уже был, +{stats.DUP_REFUND['mythic']} ♠️" if dup else " Надеть: /collection в личке с ботом"
        lines.append(f"🟣 {names[u]} выбил МИФИЧЕСКИЙ предмет за победу: {stats.item_title(item)}! 🎉{tail}")
    for u, (em, dup) in emotes.items():
        tail = f" Такая уже была, +{stats.EMOTE_DUP_REFUND['mythic']} ♠️" if dup else " Использовать можно в игре кнопкой ☺"
        lines.append(f"🟣 {names[u]} выбил МИФИЧЕСКУЮ эмоцию за победу: {stats.emote_title(em)}! 🎉{tail}")
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
# Всё меню построено на inline-кнопках. Нажатие кнопки ИЗМЕНЯЕТ то же сообщение,
# поэтому чат не засоряется. Слэш-команды тоже работают и открывают те же экраны.

_ME = None


async def _bot_me():
    global _ME
    if _ME is None:
        _ME = await bot.get_me()
    return _ME


def _name(u):
    return (u.first_name or u.username or str(u.id))[:20]


def btn(text, data=None, url=None):
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


BACK = btn("⬅️ Назад", "m:main")
LINE = "━━━━━━━━━━━━━━"
PRIVATE_ONLY = {"shop", "chests", "collection"}


async def _player(uid):
    if not stats.pool:
        return None
    try:
        return await stats.me(uid)
    except Exception:
        return None


def _bonus_on(r):
    """Разовая награда ещё доступна?"""
    return bool(stats.pool) and not (r and r["bonus_claimed"])


def _wheel_ready(r):
    if not r or not r["last_spin"]:
        return True
    return datetime.now(timezone.utc) - r["last_spin"] >= stats.SPIN_COOLDOWN


def _wait_text(r):
    left = stats.SPIN_COOLDOWN - (datetime.now(timezone.utc) - r["last_spin"])
    mins = max(int(left.total_seconds() // 60), 1)
    return f"{mins // 60} ч {mins % 60} мин"


def _bar(n, total, size=10):
    k = round(n / total * size) if total else 0
    return "▰" * k + "▱" * (size - k)


# ---------------------------------------------------------------- экраны

async def build(screen, user, private=True):
    """Возвращает (текст HTML, клавиатура) для экрана меню."""
    uid = user.id
    if screen in PRIVATE_ONLY and not private:
        return ("🔒 <b>Этот раздел работает в личке с ботом.</b>\nОткройте чат с ботом и нажмите /start.",
                kb([BACK]))
    if screen != "main" and screen != "group" and screen != "global" and not stats.pool:
        return ("⚠️ Раздел пока недоступен.", kb([BACK]))
    fn = SCREENS.get(screen, _s_main)
    return await fn(user)


async def _s_main(user):
    r = await _player(user.id)
    me = await _bot_me()
    text = ("🃏 <b>PLAYCARD</b>\n"
            "Дурак и Бураказел: с друзьями или со всем миром\n"
            f"{LINE}\n")
    if r:
        games = r["wins"] + r["losses"]
        text += (f"👤 <b>{esc(_name(user))}</b>\n"
                 f"{stats.league_title(stats.league_of(r['rating'], r.get('league')))}  ·  🏆 <b>{r['rating']}</b>\n"
                 f"🎯 Побед <b>{r['wins']}</b> из {games}\n"
                 f"♠️ <b>{r['coins']}</b>   🃏 <b>{r['jokers']}</b>\n")
    else:
        text += f"👤 <b>{esc(_name(user))}</b>\n"
    text += f"{LINE}\nВыберите, что делать 👇"
    play = f"https://t.me/{me.username}/{APP_NAME}?startapp={GLOBAL}"
    bot_url = f"https://t.me/{me.username}/{APP_NAME}?startapp=bot"
    wheel = "🎡 Колесо 🟢" if _wheel_ready(r) else "🎡 Колесо"
    try:
        fi = await stats.farm_info(user.id) if stats.pool else None
    except Exception:
        fi = None
    farm_lbl = "🌾 Ферма 🟢" if fi and fi["stored"] >= fi["rate"] else "🌾 Ферма"
    lg = stats.LEAGUES[stats.league_of(r["rating"], r.get("league"))] if r else stats.LEAGUES[0]
    rows = [
        [btn("🌍 Играть со всеми", url=play)],
        [btn(f"{lg[0]} Моя лига: {lg[1]}", "m:leagues")],
        [btn("🤖 Против бота", url=bot_url), btn("👥 В группе", "m:group")],
        [btn("🏆 Топ", "m:top"), btn("👤 Профиль", "m:me")],
        [btn(wheel, "m:wheel"), btn("🎁 Сундуки", "m:chests")],
        [btn("🎴 Коллекция", "m:collection"), btn("🏅 Достижения", "m:ach")],
        [btn(farm_lbl, "m:farm"), btn("🛍 Магазин", "m:shop")],
    ]
    if _bonus_on(r):
        rows.append([btn(f"🎁 Получить {stats.START_COINS} ♠️ в подарок", "bonus:main")])
    return text, kb(*rows)


async def _s_group(user):
    me = await _bot_me()
    text = ("👥 <b>Играть в группе</b>\n"
            f"{LINE}\n"
            "1️⃣ Добавьте меня в группу\n"
            "2️⃣ Напишите в группе /play\n"
            "3️⃣ Нажмите «Играть» и выберите Дурак или Бураказел\n\n"
            "Играть смогут только участники этой группы.")
    return text, kb([btn("➕ Добавить в группу", url=f"https://t.me/{me.username}?startgroup=true")], [BACK])


async def _s_bot(user):
    me = await _bot_me()
    url = f"https://t.me/{me.username}/{APP_NAME}?startapp=bot"
    text = ("🤖 <b>Игра против бота</b>\n"
            f"{LINE}\n"
            "Бот играет в Дурака всерьёз: считает карты, отбивается самой дешёвой картой, "
            "берёт, когда это выгодно, подкидывает по делу и переводит в переводном режиме.\n\n"
            + ("🎁 Рейтинг и награды за партии с ботом засчитываются." if BOT_REWARDS
               else "🎯 Это тренировка: партии с ботом не меняют рейтинг и не дают наград."))
    return text, kb([btn("🤖 Играть с ботом", url=url)], [BACK])


async def _s_farm(user):
    fi = await stats.farm_info(user.id)
    r = await _player(user.id)
    full = fi["stored"] >= fi["cap"]
    text = (f"🌾 <b>Ферма</b>  ·  {fi['icon']} <b>{fi['name']}</b>\n{LINE}\n"
            f"Уровень: <b>{fi['level']}/{len(stats.FARM)}</b>\n"
            f"💰 Доход: <b>{fi['rate']}</b> ♠️ в час\n"
            f"📦 Накопилось: <b>{fi['stored']}</b> / {fi['cap']} ♠️\n{_bar(fi['stored'], fi['cap'])}\n")
    if full:
        text += "⚠️ Хранилище полное: заберите доход, иначе он перестанет копиться.\n"
    else:
        m = fi["full_in_min"]
        text += f"⏳ Заполнится через {m // 60} ч {m % 60} мин\n"
    text += LINE + "\n"
    if fi["max"]:
        text += "🌟 Максимальный уровень. Ваша империя работает на полную."
    else:
        text += (f"⬆️ Улучшение: {fi['next_name']}\n"
                 f"+{fi['next_rate'] - fi['rate']} ♠️ в час, стоит <b>{_fmt(fi['up_cost'])}</b> ♠️")
    if r:
        text += f"\n\n♠️ У вас: <b>{_fmt(r['coins'])}</b>"
    rows = []
    if fi["stored"] >= 1:
        rows.append([btn(f"💰 Забрать {fi['stored']} ♠️", "farm:claim")])
    if not fi["max"]:
        rows.append([btn(f"⬆️ Улучшить за {_fmt(fi['up_cost'])} ♠️", "farm:up")])
    rows.append([BACK])
    return text, kb(*rows)


async def _s_global(user):
    me = await _bot_me()
    url = f"https://t.me/{me.username}/{APP_NAME}?startapp={GLOBAL}"
    return ("🌍 <b>Общий стол</b>\nВас посадят к другим игрокам со всего мира.",
            kb([btn("🌍 Играть со всеми", url=url)], [BACK]))


async def _s_top(user):
    rows = await stats.top()
    if not rows:
        return "🏆 <b>Топ игроков</b>\n\nПока никто не сыграл.", kb([BACK])
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"🏆 <b>Топ игроков</b>\n{LINE}"]
    for i, r in enumerate(rows, 1):
        ic = stats.LEAGUES[stats.league_of(r['rating'], r.get('league'))][0]
        lines.append(f"{medals.get(i, f'<b>{i}.</b>')} {ic} {esc(r['name'] or '—')}  ·  <b>{r['rating']}</b>  ·  {r['wins']} поб.")
    return "\n".join(lines), kb([btn("🏅 Лиги", "m:leagues"), btn("🔄 Обновить", "m:top")], [BACK])


async def _s_me(user):
    r = await _player(user.id)
    if not r:
        text = "👤 <b>Профиль</b>\n\nВы ещё не играли. Сыграйте первую партию!"
        rows = []
    else:
        games = r["wins"] + r["losses"]
        pct = round(r["wins"] / games * 100) if games else 0
        have = await stats.earned(user.id)
        text = (f"👤 <b>{esc(_name(user))}</b>\n{LINE}\n"
                f"{_league_block(r)}\n{LINE}\n"
                f"🎯 Победы: <b>{r['wins']}</b>   💀 Поражения: <b>{r['losses']}</b>\n"
                f"📊 Винрейт: <b>{pct}%</b>   🔥 Серия: <b>{r['win_streak'] or 0}</b>\n"
                f"🏅 Достижения: <b>{len(have)}/{len(stats.ACH)}</b>\n{LINE}\n"
                f"♠️ Козыри: <b>{r['coins']}</b>\n"
                f"🃏 Джокеры: <b>{r['jokers']}</b>")
        rows = []
    if _bonus_on(r):
        rows.append([btn(f"🎁 Получить {stats.START_COINS} ♠️ в подарок", "bonus:me")])
    rows.append([btn("🏅 Лига и награды", "m:leagues")])
    rows.append([BACK])
    return text, kb(*rows)


def _fmt(n):
    return f"{n:,}".replace(",", " ")


def _league_block(r):
    """Лига игрока: шкала до следующей, награда и «подушка»."""
    rating = r["rating"]
    idx = stats.league_of(rating, r.get("league"))
    L = stats.LEAGUES
    text = f"🏆 Рейтинг: <b>{_fmt(rating)}</b>\n🏅 Лига: <b>{stats.league_title(idx)}</b> ({idx + 1}/{len(L)})\n"
    if idx + 1 < len(L):
        lo, hi = L[idx][2], L[idx + 1][2]
        done = min(max(rating - lo, 0), hi - lo)
        text += (f"{_bar(done, hi - lo)} {_fmt(rating)} / {_fmt(hi)}\n"
                 f"До лиги {stats.league_title(idx + 1)}: <b>{_fmt(max(hi - rating, 0))}</b> очков\n"
                 f"🎁 Награда за вход: {stats.league_reward_text(idx + 1)}")
    else:
        text += "👑 Высшая лига! Выше только слава."
    if idx > 0:
        floor = L[idx][2] - L[idx][3]
        text += f"\n🛟 Из лиги не вылетите, пока рейтинг ≥ <b>{_fmt(floor)}</b>"
    return text


async def _s_leagues(user):
    r = await _player(user.id)
    cur = stats.league_of(r["rating"], r.get("league")) if r else 0
    reached = max(cur, (r.get("league_max") or 0) if r else 0)
    if r:
        head = f"🏅 <b>Моя лига</b>\n{LINE}\n{_league_block(r)}\n{LINE}\n"
    else:
        head = f"🏅 <b>Лиги</b>\n{LINE}\n"
    lines = [head + "<b>Все лиги и награды за вход:</b>"]
    for i, (ic, name, thr, cush) in enumerate(stats.LEAGUES):
        mark = "👉" if i == cur else ("✅" if i <= reached else "🔒")
        lines.append(f"{mark} {ic} <b>{name}</b> — от {_fmt(thr)}"
                     + (f"\n      🎁 {stats.league_reward_text(i)}" if i else ""))
    lines.append("\n🛟 <b>Подушка:</b> в лиге вы остаётесь, пока рейтинг не упадёт ниже её порога "
                 "на 100–300 очков (чем выше лига, тем больше). Награда за вход выдаётся один раз.")
    return "\n".join(lines), kb([btn("🏆 Топ", "m:top"), BACK])


async def _s_ach(user):
    have = await stats.earned(user.id)
    total = len(stats.ACH)
    done, todo = [], []
    for aid, (icon, title, desc, coins, jokers, _c) in stats.ACH.items():
        if aid in have:
            done.append(f"✅ {icon} <b>{title}</b>")
        else:
            todo.append(f"🔒 {title}\n      <i>{desc}</i> · {stats._reward(coins, jokers)}")
    text = f"🏅 <b>Достижения</b>  {len(have)}/{total}\n{_bar(len(have), total)}\n{LINE}\n"
    if done:
        text += "\n".join(done) + "\n\n"
    if todo:
        text += "\n".join(todo)
    need = stats.SECRET_NEED
    n = sum(1 for k in need if k in have)
    text = text.strip() + f"\n\n🔴 <b>Секрет:</b> за полный набор достижений (кроме 💖 и 🎰) ждёт особая награда  {n}/{len(need)}"
    return text, kb([BACK])


async def _s_wheel(user):
    r = await _player(user.id)
    total = sum(w[3] for w in stats.WHEEL)
    prizes = "\n".join(f"• {w[0]} — {w[3] / total * 100:g}%" for w in stats.WHEEL)
    text = f"🎡 <b>Колесо фортуны</b>\nОдна бесплатная попытка в сутки.\n{LINE}\n{prizes}\n{LINE}\n"
    if _wheel_ready(r):
        text += "🟢 <b>Попытка готова!</b>"
        return text, kb([btn("🎡 Крутить колесо", "spin")], [BACK])
    text += f"⏳ Следующая попытка через <b>{_wait_text(r)}</b>"
    return text, kb([BACK])


async def _s_shop(user):
    r = await _player(user.id)
    bal = f"\nУ вас: <b>{r['jokers']}</b> 🃏" if r else ""
    text = ("🛍 <b>Магазин</b>\n"
            f"{LINE}\n"
            "🃏 <b>Джокеры</b> — донатная валюта. Тратятся на золотые сундуки.\n"
            f"Оплата в Telegram Stars ⭐{bal}")
    rows = [[btn(f"🃏 {n} Джокеров — {p} ⭐", f"buy:{n}")] for n, p in PACKS.items()]
    rows.append([BACK])
    return text, kb(*rows)


def _odds(odds):
    return " ".join(f"{stats.RARITY[r][0]}{p}%" for r, p in odds.items() if p)


async def _s_chests(user):
    uid = user.id
    await stats.ensure(uid, _name(user))
    r = await _player(uid)
    stock = await stats.chest_stock(uid)
    lines = ["🎁 <b>Сундуки</b>\nВ них колоды и столы (только внешний вид).", LINE]
    if r:
        lines.append(f"♠️ <b>{r['coins']}</b>   🃏 <b>{r['jokers']}</b>\n")
    rows = []
    if stock:
        lines.append("🎉 <b>Ваши сундуки за победы:</b>")
        for k, n in stock.items():
            lines.append(f"   {stats.CHESTS[k][0]} {stats.CHESTS[k][1]} × {n}")
            rows.append([btn(f"{stats.CHESTS[k][0]} Открыть бесплатно (×{n})", f"open:{k}")])
        lines.append("")
    for cid, (icon, title, cur, price, odds) in stats.CHESTS.items():
        sym = "♠️" if cur == "coins" else "🃏"
        lines.append(f"{icon} <b>{title}</b> — {price} {sym}\n      {_odds(odds)}")
        rows.append([btn(f"{icon} {title} · {price} {sym}", f"chest:{cid}")])
    lines.append(f"\n🍀 Шанс сундука за победу: " + ", ".join(
        f"{stats.CHESTS[k][0]} {p:g}%" for k, p in stats.DROP_CHANCE.items()))
    lines.append("♻️ Дубликат вернёт Козыри: " + " ".join(
        f"{stats.RARITY[r_][0]}{v}" for r_, v in stats.DUP_REFUND.items()))
    ec = stats.EMOTE_CHEST
    lines.append(f"\n{ec[0]} <b>{ec[1]}</b> — {ec[3]} ♠️\n      {_odds(ec[4])}\n      Новые анимированные эмоции для игры (кнопка ☺).")
    rows.append([btn(f"{ec[0]} {ec[1]} · {ec[3]} ♠️", "echest")])
    rows.append([BACK])
    return "\n".join(lines), kb(*rows)


async def _s_collection(user):
    uid = user.id
    have = await stats.owned_items(uid)
    cur = await stats.look(uid)
    worn = set(cur.values())
    droppable = [i for r in stats.POOL.values() for i in r]
    lines = [f"🎴 <b>Коллекция</b>  {len(have & set(droppable))}/{len(droppable)}", LINE]
    for r, (ic, name) in stats.RARITY.items():
        got = [i for i in stats.POOL[r] if i in have]
        lines.append(f"{ic} <b>{name}</b>  {len(got)}/{len(stats.POOL[r])}")
        for i in got:
            lines.append(f"   {'✅' if i in worn else '▫️'} {stats.ITEMS[i][3]} {stats.ITEMS[i][2]} "
                         f"({'колода' if stats.ITEMS[i][0] == 'deck' else 'стол'})")
    my_em = await stats.owned_emotes(uid)
    extra = [k for k in stats.EMOTES if stats.EMOTES[k][0] != "base"]
    got_em = [k for k in extra if k in my_em]
    lines.append(f"\n🎭 <b>Эмоции</b>  {len(got_em)}/{len(extra)}")
    if got_em:
        lines.append("   " + ", ".join(stats.emote_title(k) for k in got_em))
    lines.append("\nНажмите на предмет, чтобы надеть его 👇")
    items = [btn(("✅ " if i in worn else "") + stats.ITEMS[i][3] + " " + stats.ITEMS[i][2], f"eq:{i}")
             for i in stats.ITEMS if i in have]
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    rows.append([BACK])
    return "\n".join(lines), kb(*rows)


SCREENS = {"main": _s_main, "farm": _s_farm, "bot": _s_bot, "leagues": _s_leagues, "group": _s_group, "global": _s_global, "top": _s_top, "me": _s_me,
           "ach": _s_ach, "wheel": _s_wheel, "shop": _s_shop, "chests": _s_chests,
           "collection": _s_collection}


async def _show(q, text, markup):
    """Меняет то же сообщение (если не вышло, шлёт новое)."""
    try:
        await q.message.edit_text(text, reply_markup=markup, parse_mode="HTML",
                                  disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await q.message.answer(text, reply_markup=markup, parse_mode="HTML",
                                   disable_web_page_preview=True)


async def _send(m: Message, screen):
    text, markup = await build(screen, m.from_user, m.chat.type == "private")
    await m.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


# ---------------------------------------------------------------- команды

@dp.message(Command("start"))
async def cmd_start(m: Message):
    if stats.pool:
        await stats.ensure(m.from_user.id, _name(m.from_user))
    await _send(m, "main")


def _reg(cmd, screen):
    @dp.message(Command(cmd))
    async def handler(m: Message):
        await _send(m, screen)
    return handler


for _cmd, _screen in [("menu", "main"), ("farm", "farm"), ("bot", "bot"), ("leagues", "leagues"), ("global", "global"), ("top", "top"), ("me", "me"),
                      ("achievements", "ach"), ("wheel", "wheel"), ("shop", "shop"),
                      ("chests", "chests"), ("collection", "collection")]:
    _reg(_cmd, _screen)


@dp.message(Command("bonus"))
async def cmd_bonus(m: Message):
    r = await _player(m.from_user.id)
    if _bonus_on(r):
        return await m.answer(f"🎁 Разовая награда: <b>{stats.START_COINS}</b> ♠️ Козырей.",
                              reply_markup=kb([btn("🎁 Получить", "bonus:main")]), parse_mode="HTML")
    await m.answer("Вы уже получили разовую награду.")


@dp.message(Command("play"))
async def cmd_play(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        await m.answer("Эта команда работает в группе. Добавьте меня в группу и напишите /play там.")
        return
    me = await _bot_me()
    url = f"https://t.me/{me.username}/{APP_NAME}?startapp={m.chat.id}"
    markup = kb([btn("🃏 Играть", url=url)],
                [btn("🌍 Играть со всеми", url=f"https://t.me/{me.username}/{APP_NAME}?startapp={GLOBAL}")])
    await m.answer("🃏 <b>Стол готов!</b>\nИграть могут только участники этой группы.",
                   reply_markup=markup, parse_mode="HTML")


@dp.message(Command("paysupport"))
async def cmd_paysupport(m: Message):
    await m.answer(f"Вопросы по оплате и возвратам: напишите {SUPPORT}.")


# ---------------------------------------------------------------- кнопки

@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(q: CallbackQuery):
    if not q.message:
        return await q.answer()
    await q.answer()
    screen = q.data[2:]
    text, markup = await build(screen, q.from_user, q.message.chat.type == "private")
    await _show(q, text, markup)


@dp.callback_query(F.data.startswith("bonus:"))
async def cb_bonus(q: CallbackQuery):
    if not stats.pool:
        return await q.answer("Пока недоступно", show_alert=True)
    bal = await stats.claim_bonus(q.from_user.id, _name(q.from_user))
    if bal is None:
        await q.answer("Вы уже получили эту награду", show_alert=True)
    else:
        await q.answer(f"🎁 +{stats.START_COINS} ♠️ Козырей!", show_alert=True)
    if q.message:
        screen = q.data.split(":", 1)[1]
        text, markup = await build(screen if screen in SCREENS else "main", q.from_user,
                                   q.message.chat.type == "private")
        await _show(q, text, markup)


@dp.callback_query(F.data.startswith("farm:"))
async def cb_farm(q: CallbackQuery):
    if not q.message:
        return await q.answer()
    if not stats.pool:
        return await q.answer("Недоступно", show_alert=True)
    uid = q.from_user.id
    await stats.ensure(uid, _name(q.from_user))
    act = q.data.split(":", 1)[1]
    if act == "claim":
        res = await stats.farm_claim(uid)
        if res.get("error") == "empty":
            await q.answer("Пока нечего забирать", show_alert=True)
        else:
            await q.answer(f"💰 +{res['stored']} ♠️ с фермы")
    elif act == "up":
        res = await stats.farm_upgrade(uid)
        if res.get("error") == "funds":
            await q.answer(f"Не хватает Козырей: нужно {res['cost']} ♠️", show_alert=True)
        elif res.get("error") == "max":
            await q.answer("Ферма уже на максимальном уровне", show_alert=True)
        else:
            await q.answer(f"⬆️ Ферма улучшена до уровня {res['level']}!", show_alert=True)
    else:
        await q.answer()
    text, markup = await build("farm", q.from_user, True)
    await _show(q, text, markup)


@dp.callback_query(F.data == "spin")
async def cb_spin(q: CallbackQuery):
    if not q.message:
        return await q.answer()
    if not stats.pool:
        return await q.answer("Колесо пока недоступно", show_alert=True)
    res = await stats.spin(q.from_user.id, _name(q.from_user))
    if "wait" in res:
        await q.answer("⏳ Колесо уже крутили сегодня", show_alert=True)
        text, markup = await build("wheel", q.from_user, True)
        return await _show(q, text, markup)
    await q.answer()
    await _show(q, "🎡 <b>Колесо крутится...</b>", None)
    await asyncio.sleep(2)
    text = (f"🎡 <b>Выпало: {res['label']}!</b>\n{LINE}\n"
            f"♠️ Козыри: <b>{res['coins']}</b>\n🃏 Джокеры: <b>{res['jokers']}</b>")
    for a in res["ach"]:
        text += f"\n\n🏅 <b>Достижение:</b> {stats.describe(a)}"
    await _show(q, text, kb([BACK]))


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
    bal = await stats.add_jokers(m.from_user.id, _name(m.from_user), n, sp.total_amount,
                                 sp.telegram_payment_charge_id)
    if bal is None:
        return
    bal, ach = bal
    text = f"✅ <b>Спасибо!</b> Начислено {n} 🃏 Джокеров.\nТеперь у вас: <b>{bal}</b>"
    for a in ach:
        text += f"\n\n🏅 <b>Достижение:</b> {stats.describe(a)}"
    await m.answer(text, reply_markup=kb([btn("🎁 К сундукам", "m:chests")], [btn("🏠 Меню", "m:main")]),
                   parse_mode="HTML")


@dp.callback_query(F.data.startswith("chest:"))
async def cb_chest(q: CallbackQuery):
    await _open_chest_cb(q, False)


@dp.callback_query(F.data.startswith("open:"))
async def cb_open_stock(q: CallbackQuery):
    await _open_chest_cb(q, True)


async def _open_chest_cb(q: CallbackQuery, stock: bool):
    if not q.message:
        return await q.answer()
    if not stats.pool:
        return await q.answer("Недоступно", show_alert=True)
    kind = q.data.split(":", 1)[1]
    res = await stats.open_chest(q.from_user.id, kind, stock=stock)
    if res.get("error") == "funds":
        return await q.answer("Не хватает валюты. Колесо, игры и магазин помогут", show_alert=True)
    if res.get("error") == "nostock":
        return await q.answer("Таких сундуков больше нет. Выигрывайте партии!", show_alert=True)
    if "error" in res:
        return await q.answer("Недоступно", show_alert=True)
    await q.answer()
    await _show(q, "🎁 <b>Открываем сундук...</b>", None)
    await asyncio.sleep(2)
    text = f"🎁 <b>Вам выпало:</b>\n{stats.item_title(res['item'])}"
    if res["dup"]:
        text += f"\n\n♻️ Уже есть. Возвращаем <b>+{res['refund']}</b> ♠️"
    text += f"\n{LINE}\n♠️ Козыри: <b>{res['coins']}</b>\n🃏 Джокеры: <b>{res['jokers']}</b>"
    for a in res["ach"]:
        text += f"\n\n🏅 <b>Достижение:</b> {stats.describe(a)}"
    rows = [[btn("🎁 Ещё сундуки", "m:chests")]]
    if not res["dup"]:
        rows[0].append(btn("🎴 Надеть", "m:collection"))
    rows.append([btn("🏠 Меню", "m:main")])
    await _show(q, text, kb(*rows))


@dp.callback_query(F.data == "echest")
async def cb_echest(q: CallbackQuery):
    if not q.message:
        return await q.answer()
    if not stats.pool:
        return await q.answer("Недоступно", show_alert=True)
    res = await stats.open_emote_chest(q.from_user.id)
    if res.get("error") == "funds":
        return await q.answer(f"Не хватает Козырей: нужно {stats.EMOTE_CHEST[3]} ♠️", show_alert=True)
    if "error" in res:
        return await q.answer("Недоступно", show_alert=True)
    await q.answer()
    await _show(q, "🎭 <b>Открываем сундук эмоций...</b>", None)
    await asyncio.sleep(2)
    text = f"🎭 <b>Вам выпала эмоция:</b>\n{stats.emote_title(res['emote'])}"
    if res["dup"]:
        text += f"\n\n♻️ Уже есть. Возвращаем <b>+{res['refund']}</b> ♠️"
    else:
        text += "\n\nИспользовать можно в игре кнопкой ☺"
    text += f"\n{LINE}\n♠️ Козыри: <b>{res['coins']}</b>\n🃏 Джокеры: <b>{res['jokers']}</b>"
    await _show(q, text, kb([btn("🎭 Ещё сундук", "echest")], [btn("🎁 К сундукам", "m:chests"), btn("🏠 Меню", "m:main")]))


@dp.callback_query(F.data.startswith("eq:"))
async def cb_equip(q: CallbackQuery):
    if not q.message:
        return await q.answer()
    if not stats.pool:
        return await q.answer("Недоступно", show_alert=True)
    if not await stats.equip(q.from_user.id, q.data.split(":", 1)[1]):
        return await q.answer("Этого предмета у вас нет", show_alert=True)
    await q.answer("✅ Надето! Сменить можно и прямо в игре")
    text, markup = await build("collection", q.from_user, True)
    await _show(q, text, markup)


# ---------------------------------------------------------------- сервер


COMMANDS = [
    BotCommand(command="start", description="🏠 Главное меню"),
    BotCommand(command="global", description="🌍 Играть со всеми"),
    BotCommand(command="bot", description="🤖 Играть против бота"),
    BotCommand(command="farm", description="🌾 Ферма: пассивный доход"),
    BotCommand(command="top", description="🏆 Рейтинг игроков"),
    BotCommand(command="me", description="👤 Мой профиль"),
    BotCommand(command="wheel", description="🎡 Колесо фортуны"),
    BotCommand(command="chests", description="🎁 Сундуки"),
    BotCommand(command="collection", description="🎴 Колоды и столы"),
    BotCommand(command="achievements", description="🏅 Достижения"),
    BotCommand(command="shop", description="🛍 Магазин Джокеров"),
]


@asynccontextmanager
async def lifespan(app):
    await stats.init()
    try:
        await bot.set_my_commands(COMMANDS)   # кнопка «Меню» слева от поля ввода
    except Exception as e:
        print("commands error:", e)
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
            start = data["start_param"]
            is_global = start == GLOBAL
            is_bot = start == "bot"
            chat_id = None if (is_global or is_bot) else int(start)
        except Exception:
            await ws.send_json({"fatal": "Откройте игру кнопкой: в группе (/play) или в личке с ботом (/global)."})
            await ws.close()
            return
        uid = user["id"]
        if not is_global and not is_bot and not await is_member(chat_id, uid):
            await ws.send_json({"fatal": "Играть могут только участники этой группы."})
            await ws.close()
            return
        room = solo_room(uid) if is_bot else (pick_global_room(uid) if is_global else ROOMS.setdefault(chat_id, Room(chat_id)))
        name = (user.get("first_name") or user.get("username") or str(uid))[:20]
        await stats.ensure(uid, name)
        look = await stats.look(uid)
        my_emotes = await stats.owned_emotes(uid)
        async with room.lock:
            if room.public:
                ROOMS[room.chat_id] = room
            was_empty = not room.conns
            room.conns.setdefault(uid, set()).add(ws)
            if room.host is None:
                room.host = uid
            room.ensure_host()
            room.owned_emotes[uid] = my_emotes
            if was_empty:      # стол открыли заново: оформление берём у первого вошедшего
                room.skin = dict(look)
            await room.broadcast()
        await ws.send_json({"emotes": sorted(my_emotes)})
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict) or msg.get("t") == "ping":
                continue
            if msg.get("t") == "emotes":     # обновить список открытых эмоций (например, после сундука)
                my_emotes = await stats.owned_emotes(uid)
                async with room.lock:
                    room.owned_emotes[uid] = my_emotes
                await ws.send_json({"emotes": sorted(my_emotes)})
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
                if room.public and not room.conns:
                    asyncio.create_task(drop_later(room))
