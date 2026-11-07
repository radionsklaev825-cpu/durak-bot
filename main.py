import asyncio
import hashlib
import hmac
import json
import os
import random
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

TOKEN = os.environ["BOT_TOKEN"]
APP_NAME = os.getenv("APP_NAME", "play")  # короткое имя мини-аппа из BotFather
BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL", "")
SECRET = hashlib.sha256(("wh:" + TOKEN).encode()).hexdigest()[:32]
HERE = os.path.dirname(os.path.abspath(__file__))

bot = Bot(TOKEN)
dp = Dispatcher()

# ---------------------------------------------------------------- игра

RANKS = ["6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = ["♠", "♥", "♦", "♣"]


def rk(c):
    return RANKS.index(c[:-1])


def st(c):
    return c[-1]


class GameError(Exception):
    pass


class Room:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.conns = {}  # user_id -> set(websocket)
        self.lock = asyncio.Lock()
        self.players = []
        self.reset()

    def reset(self):
        self.phase = "lobby"  # lobby | play | over
        self.deck = []
        self.trump = None
        self.table = []  # [[атака, защита|None], ...]
        self.taking = False
        self.passed = set()
        self.attacker = 0
        self.defender = 1
        self.loser = None
        self.log = "Ждём игроков"
        for p in self.players:
            p["hand"] = []
            p["out"] = False

    # --- вспомогательное
    def idx(self, uid):
        for i, p in enumerate(self.players):
            if p["id"] == uid:
                return i
        return None

    def next_active(self, i):
        n = len(self.players)
        j = i
        for _ in range(n):
            j = (j + 1) % n
            if not self.players[j]["out"]:
                return j
        return i

    def beats(self, a, d):
        if st(a) == st(d):
            return rk(d) > rk(a)
        return st(d) == self.trump and st(a) != self.trump

    def unbeaten(self):
        return [t for t in self.table if t[1] is None]

    def can_attack_with(self, p, c):
        if p == self.defender or self.players[p]["out"]:
            return False
        if not self.table:
            return p == self.attacker
        ranks = {rk(x) for pair in self.table for x in pair if x}
        dhand = len(self.players[self.defender]["hand"])
        return (
            len(self.table) < 6
            and len(self.unbeaten()) + 1 <= dhand
            and rk(c) in ranks
        )

    # --- ход игры
    def start(self):
        if len(self.players) < 2:
            raise GameError("Нужно минимум 2 игрока")
        deck = [r + s for s in SUITS for r in RANKS]
        random.shuffle(deck)
        self.deck = deck
        self.trump = st(deck[0])
        for p in self.players:
            p["out"] = False
            p["hand"] = [self.deck.pop() for _ in range(6)]
        best = None
        for i, p in enumerate(self.players):
            for c in p["hand"]:
                if st(c) == self.trump and (best is None or rk(c) < best[0]):
                    best = (rk(c), i)
        self.attacker = best[1] if best else random.randrange(len(self.players))
        self.defender = self.next_active(self.attacker)
        self.table, self.taking, self.passed, self.loser = [], False, set(), None
        self.phase = "play"
        self.log = f"Игра началась. Ходит {self.players[self.attacker]['name']}"

    def attack(self, p, card):
        if p is None or self.phase != "play":
            raise GameError("Сейчас нельзя")
        if p == self.defender:
            raise GameError("Вы отбиваетесь")
        if card not in self.players[p]["hand"]:
            raise GameError("Нет такой карты")
        if not self.table and p != self.attacker:
            raise GameError("Сейчас ходит другой игрок")
        if not self.can_attack_with(p, card):
            raise GameError("Эту карту нельзя подкинуть")
        self.players[p]["hand"].remove(card)
        self.table.append([card, None])
        self.passed = set()
        self.log = f"{self.players[p]['name']} ходит {card}"
        self.check_finish()

    def defend(self, p, card, target=None):
        if p is None or p != self.defender or self.taking or self.phase != "play":
            raise GameError("Сейчас нельзя отбиваться")
        if card not in self.players[p]["hand"]:
            raise GameError("Нет такой карты")
        pairs = self.unbeaten()
        if target:
            pairs = [t for t in pairs if t[0] == target]
        for t in pairs:
            if self.beats(t[0], card):
                t[1] = card
                break
        else:
            raise GameError("Этой картой не побить")
        self.players[p]["hand"].remove(card)
        self.passed = set()
        self.log = f"{self.players[p]['name']} бьёт {card}"
        self.check_finish()

    def take(self, p):
        if p is None or p != self.defender or self.taking or not self.unbeaten():
            raise GameError("Сейчас нельзя взять карты")
        self.taking = True
        self.passed = set()
        self.log = f"{self.players[p]['name']} берёт карты. Можно подкинуть"
        self.check_finish()

    def done(self, p):
        if p is None or p == self.defender or not self.table or self.phase != "play":
            raise GameError("Сейчас нельзя")
        if not self.taking and self.unbeaten():
            raise GameError("Дождитесь, пока игрок отобьётся")
        self.passed.add(p)
        self.check_finish()

    def check_finish(self):
        if self.phase != "play" or not self.table:
            return
        d = self.players[self.defender]
        if not self.taking:
            if self.unbeaten():
                return
            if len(self.table) >= 6 or not d["hand"]:
                return self.finish_round(False)
        for i, pl in enumerate(self.players):
            if i == self.defender or pl["out"] or i in self.passed:
                continue
            if any(self.can_attack_with(i, c) for c in pl["hand"]):
                return
        self.finish_round(self.taking)

    def draw_up(self):
        n = len(self.players)
        order = [
            (self.attacker + k) % n
            for k in range(n)
            if (self.attacker + k) % n != self.defender
        ]
        order.append(self.defender)
        for j in order:
            hand = self.players[j]["hand"]
            while len(hand) < 6 and self.deck:
                hand.append(self.deck.pop())

    def finish_round(self, taken):
        d = self.players[self.defender]
        cards = [c for pair in self.table for c in pair if c]
        if taken:
            d["hand"].extend(cards)
            self.log = f"{d['name']} забрал карты"
        else:
            self.log = "Бито"
        self.table, self.taking, self.passed = [], False, set()
        self.draw_up()
        for p in self.players:
            if not p["hand"] and not self.deck:
                p["out"] = True
        alive = [i for i, p in enumerate(self.players) if not p["out"]]
        if len(alive) <= 1:
            self.phase = "over"
            self.loser = self.players[alive[0]]["id"] if alive else None
            return
        if taken or self.players[self.defender]["out"]:
            self.attacker = self.next_active(self.defender)
        else:
            self.attacker = self.defender
        self.defender = self.next_active(self.attacker)

    def handle(self, uid, name, m):
        t = m.get("t")
        p = self.idx(uid)
        card = m.get("card")
        if t in ("attack", "defend") and not isinstance(card, str):
            raise GameError("Некорректная карта")
        if t == "join":
            if self.phase != "lobby":
                raise GameError("Игра уже идёт")
            if p is None:
                if len(self.players) >= 6:
                    raise GameError("За столом максимум 6 игроков")
                self.players.append({"id": uid, "name": name, "hand": [], "out": False})
                self.log = f"{name} сел за стол"
        elif t == "leave":
            if self.phase != "lobby" or p is None:
                raise GameError("Выйти можно только до начала игры")
            self.players.pop(p)
        elif t == "start":
            if p is None:
                raise GameError("Сначала сядьте за стол")
            if self.phase != "lobby":
                raise GameError("Игра уже идёт")
            self.start()
        elif t == "reset":
            if p is None:
                raise GameError("Завершить игру могут только игроки")
            self.reset()
        elif t == "attack":
            self.attack(p, card)
        elif t == "defend":
            self.defend(p, card, m.get("target") if isinstance(m.get("target"), str) else None)
        elif t == "take":
            self.take(p)
        elif t == "done":
            self.done(p)

    # --- что видит игрок
    def legal(self, me):
        if self.phase != "play" or me is None or self.players[me]["out"]:
            return []
        hand = self.players[me]["hand"]
        if me == self.defender:
            if self.taking:
                return []
            un = [t[0] for t in self.unbeaten()]
            return [c for c in hand if any(self.beats(a, c) for a in un)]
        return [c for c in hand if self.can_attack_with(me, c)]

    def view(self, uid):
        me = self.idx(uid)
        hand, acts, hint = [], [], ""
        if me is not None:
            hand = sorted(
                self.players[me]["hand"],
                key=lambda c: (st(c) == self.trump, SUITS.index(st(c)), rk(c)),
            )
        if self.phase == "play" and me is not None and not self.players[me]["out"]:
            all_beaten = bool(self.table) and not self.unbeaten()
            if me == self.defender:
                if self.taking:
                    hint = "Вы берёте карты"
                elif self.unbeaten():
                    acts = ["take"]
                    hint = "Побейте карту или возьмите"
                else:
                    hint = "Ждём, пока подкинут"
            elif not self.table:
                hint = "Ваш ход, атакуйте" if me == self.attacker else "Ходит другой игрок"
            elif me not in self.passed and (self.taking or all_beaten):
                acts = ["done"]
                hint = "Подкиньте карту или завершите"
            else:
                hint = "Ждём остальных"
        return {
            "phase": self.phase,
            "me": uid,
            "trump": self.trump,
            "trump_card": self.deck[0] if self.deck else None,
            "deck": len(self.deck),
            "taking": self.taking,
            "players": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "n": len(p["hand"]),
                    "out": p["out"],
                    "att": self.phase == "play" and i == self.attacker,
                    "def": self.phase == "play" and i == self.defender,
                    "passed": i in self.passed,
                    "online": p["id"] in self.conns,
                }
                for i, p in enumerate(self.players)
            ],
            "table": self.table,
            "hand": hand,
            "legal": self.legal(me),
            "actions": acts,
            "hint": hint,
            "log": self.log,
            "loser": self.loser,
            "watching": len([u for u in self.conns if self.idx(u) is None]),
        }

    async def broadcast(self):
        for uid, sockets in list(self.conns.items()):
            payload = self.view(uid)
            for w in list(sockets):
                try:
                    await w.send_json(payload)
                except Exception:
                    sockets.discard(w)


ROOMS = {}

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
    await m.answer(
        "Привет! Я игровой бот. Добавьте меня в группу и напишите там /play, "
        "чтобы открыть стол для игры в Дурака."
    )


@dp.message(Command("play"))
async def cmd_play(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        await m.answer("Эта команда работает в группе. Добавьте меня в группу и напишите /play там.")
        return
    me = await bot.get_me()
    url = f"https://t.me/{me.username}/{APP_NAME}?startapp={m.chat.id}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🃏 Играть в Дурака", url=url)]]
    )
    await m.answer("Стол готов. Играть могут только участники этой группы.", reply_markup=kb)


# ---------------------------------------------------------------- сервер


@asynccontextmanager
async def lifespan(app):
    if BASE_URL:
        url = f"{BASE_URL}/webhook"
        try:
            info = await bot.get_webhook_info()
            if info.url != url:
                await bot.set_webhook(url, secret_token=SECRET, allowed_updates=["message"])
        except Exception as e:
            print("webhook error:", e)
    yield
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/health")
async def health():
    return {"ok": True}


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
        async with room.lock:
            room.conns.setdefault(uid, set()).add(ws)
            await room.broadcast()
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict) or msg.get("t") == "ping":
                continue
            async with room.lock:
                try:
                    room.handle(uid, name, msg)
                except GameError as e:
                    await ws.send_json({"err": str(e)})
                await room.broadcast()
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as e:
        print("ws error:", e)
    finally:
        if room is not None and uid is not None:
            async with room.lock:
                sockets = room.conns.get(uid)
                if sockets:
                    sockets.discard(ws)
                    if not sockets:
                        del room.conns[uid]
                await room.broadcast()
