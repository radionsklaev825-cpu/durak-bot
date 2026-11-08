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

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

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
            asyncio.create_task(self._run_res(eng, eng.res_id))
        if getattr(eng, "phase", None) == "board" and self._board_sched != eng.board_id:
            self._board_sched = eng.board_id
            asyncio.create_task(self._run_board(eng, eng.board_id))

    async def _run_res(self, eng, rid):
        await asyncio.sleep(2.4 if eng.kind == "durak" else 2.2)
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
        self.emit("over")
        asyncio.create_task(self.say(f"🏁 {text}"))

    # ------------------------------------------------------------ что видит игрок
    def view(self, uid):
        v = {
            "me": uid, "phase": self.phase, "game": self.game, "cfg": self.cfg, "host": self.host,
            "seats": [{"id": p["id"], "name": p["name"], "on": p["id"] in self.conns} for p in self.seated],
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
        "чтобы открыть стол: Дурак или Бураказел."
    )


@dp.message(Command("play"))
async def cmd_play(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        await m.answer("Эта команда работает в группе. Добавьте меня в группу и напишите /play там.")
        return
    me = await bot.get_me()
    url = f"https://t.me/{me.username}/{APP_NAME}?startapp={m.chat.id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🃏 Играть", url=url)]])
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
        async with room.lock:
            room.conns.setdefault(uid, set()).add(ws)
            if room.host is None:
                room.host = uid
            room.ensure_host()
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
