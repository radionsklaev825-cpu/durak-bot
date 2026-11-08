import itertools
import random

from durak import GameError

# порядок силы карт в Бураказеле: десятка выше всех, кроме туза
RANKS = ["6", "7", "8", "9", "J", "Q", "K", "10", "A"]
SUITS = ["♠", "♥", "♦", "♣"]
VAL = {"A": 11, "10": 10, "K": 4, "Q": 3, "J": 2}
COLOR = {"♠": "black", "♣": "black", "♥": "red", "♦": "red"}
SIZES = {"1x1": 2, "ffa3": 3, "2x2": 4}


def rk(c):
    return RANKS.index(c[:-1])


def st(c):
    return c[-1]


def val(c):
    return VAL.get(c[:-1], 0)


def penalty(points, ffa):
    top = 40 if ffa else 60
    if points > top:
        return 0
    if points > 30:
        return 2
    if points > 0:
        return 4
    return 6


class Bura:
    kind = "bura"

    def __init__(self, players, fmt="1x1", target=12, emit=None):
        if fmt not in SIZES or len(players) != SIZES[fmt]:
            raise GameError("Неверное число игроков")
        self.emit = emit or (lambda *a, **k: None)
        self.fmt = fmt
        self.target = 6 if int(target) == 6 else 12
        self.players = [{"id": p["id"], "name": p["name"], "hand": []} for p in players]
        self.n = len(self.players)
        self.units = 3 if fmt == "ffa3" else 2
        self.pen = [0] * self.units
        self.mult = 1
        self.deal_no = 0
        self.over = False
        self.loser_ids = []
        self.surrendered = False
        self.res = None
        self.res_id = 0
        self.board = None
        self.board_id = 0
        self.phase = "pick"      # pick | leader | play | board
        self.picks = {}
        self.leader = 0
        self.leader_team = None
        self.next_leader = 0
        self.trick = None
        self.deck = []
        self.pts = [0] * self.units
        self.trump = None
        self.trump_card = None
        self.hidden = None
        self.hidden_open = False
        self.banner = ""
        self.log = "Выберите масть" if fmt == "ffa3" else "Выберите цвет"

    # ------------------------------------------------------------ помощники
    def idx(self, uid):
        for i, p in enumerate(self.players):
            if p["id"] == uid:
                return i
        return None

    def unit(self, p):
        return p % 2 if self.fmt == "2x2" else p

    def unit_name(self, u):
        if self.fmt == "2x2":
            return f"{self.players[u]['name']} и {self.players[u + 2]['name']}"
        return self.players[u]["name"]

    def beats(self, a, c):
        if st(a) == st(c):
            return rk(c) > rk(a)
        return st(c) == self.trump and st(a) != self.trump

    def beats_all(self, top, cards):
        for perm in itertools.permutations(cards):
            if all(self.beats(a, c) for a, c in zip(top, perm)):
                return True
        return False

    def options(self):
        return list(SUITS) if self.fmt == "ffa3" else ["black", "red"]

    def owner(self, suit):
        for u, v in self.picks.items():
            if self.fmt == "ffa3":
                if v == suit or (isinstance(v, list) and suit in v):
                    return u
            elif v == COLOR[suit]:
                return u
        return 0

    # ------------------------------------------------------------ действия
    def act(self, uid, m):
        t = m.get("t")
        p = self.idx(uid)
        if self.over:
            raise GameError("Игра окончена")
        if p is None:
            raise GameError("Вы не за столом")
        if t == "pick":
            return self.pick(p, m.get("choice"))
        if t == "leader":
            return self.choose_leader(p, m.get("target"))
        if t == "continue":
            return self.continue_()
        if self.res:
            raise GameError("Подождите секунду")
        cards = m.get("cards")
        if not isinstance(cards, list) or not all(isinstance(c, str) for c in cards):
            raise GameError("Некорректные карты")
        if t == "lead":
            return self.lead(p, cards)
        if t == "respond":
            return self.respond(p, cards, m.get("mode"))
        raise GameError("Неизвестное действие")

    def pick(self, p, choice):
        if self.phase != "pick":
            raise GameError("Выбор уже сделан")
        if choice not in self.options():
            raise GameError("Нет такого варианта")
        u = self.unit(p)
        if u in self.picks:
            raise GameError("Ваша команда уже выбрала" if self.fmt == "2x2" else "Вы уже выбрали")
        if any(v == choice for v in self.picks.values() if isinstance(v, str)):
            raise GameError("Это уже занято")
        self.picks[u] = choice
        if self.fmt == "ffa3":
            if len(self.picks) == 2:
                rest = [s for s in SUITS if s not in self.picks.values()]
                third = [x for x in range(3) if x not in self.picks][0]
                self.picks[third] = rest
        else:
            self.picks[1 - u] = [o for o in self.options() if o != choice][0]
        if len(self.picks) == self.units:
            self.begin_deal(True)

    def begin_deal(self, first):
        deck = [r + s for s in SUITS for r in RANKS]
        random.shuffle(deck)
        self.deck = deck
        self.hidden = deck[0]          # закрытая карта внизу колоды
        self.trump_card = deck[1]      # открытый козырь
        self.trump = st(self.trump_card)
        self.hidden_open = False
        for p in self.players:
            p["hand"] = [self.deck.pop() for _ in range(4)]
        self.pts = [0] * self.units
        self.trick = None
        self.board = None
        self.banner = ""
        self.deal_no += 1
        self.emit("deal")
        if first:
            u = self.owner(self.trump)
            if self.fmt == "2x2":
                self.phase = "leader"
                self.leader_team = u
                self.log = "Выбираем, кто пойдёт первым"
            else:
                self.leader = u
                self.phase = "play"
                self.log = f"Первым ходит {self.players[u]['name']}"
        else:
            self.leader = self.next_leader
            self.phase = "play"
            self.log = f"Ходит {self.players[self.leader]['name']}"

    def choose_leader(self, p, target):
        if self.phase != "leader":
            raise GameError("Сейчас нельзя")
        if self.unit(p) == self.leader_team:
            raise GameError("Выбирает команда соперников")
        t = self.idx(target)
        if t is None or self.unit(t) != self.leader_team:
            raise GameError("Выберите игрока из команды, чей цвет совпал с козырем")
        self.leader = t
        self.phase = "play"
        self.log = f"Первым ходит {self.players[t]['name']}"

    def check_cards(self, p, cards):
        hand = self.players[p]["hand"]
        if len(set(cards)) != len(cards) or not all(c in hand for c in cards):
            raise GameError("Нет таких карт")

    def lead(self, p, cards):
        if self.phase != "play" or self.trick or p != self.leader:
            raise GameError("Сейчас ходит другой игрок")
        if not 1 <= len(cards) <= 4:
            raise GameError("Можно положить от 1 до 4 карт")
        self.check_cards(p, cards)
        if len({st(c) for c in cards}) != 1:
            raise GameError("Ходить нужно картами одной масти")
        for c in cards:
            self.players[p]["hand"].remove(c)
        self.trick = {
            "n": len(cards), "top": list(cards), "top_owner": p,
            "plays": [{"p": p, "cards": list(cards), "mode": "lead"}],
            "queue": [(p + k) % self.n for k in range(1, self.n)],
        }
        self.banner = ""
        if len(cards) == 4 and all(st(c) == self.trump for c in cards):
            self.banner = "БУРА!"
            self.emit("bura")
        self.log = f"{self.players[p]['name']} ходит"
        self.emit("card")

    def respond(self, p, cards, mode):
        tr = self.trick
        if self.phase != "play" or not tr or tr["queue"][0] != p:
            raise GameError("Сейчас не ваш ход")
        if len(cards) != tr["n"]:
            raise GameError(f"Нужно положить {tr['n']} карт.")
        self.check_cards(p, cards)
        if mode == "beat":
            if not self.beats_all(tr["top"], cards):
                raise GameError("Этими картами не побить")
            tr["top"], tr["top_owner"] = list(cards), p
        elif mode != "pass":
            raise GameError("Выберите: отбить или пас")
        for c in cards:
            self.players[p]["hand"].remove(c)
        tr["plays"].append({"p": p, "cards": list(cards), "mode": mode})
        tr["queue"].pop(0)
        self.log = f"{self.players[p]['name']} {'бьёт' if mode == 'beat' else 'пасует'}"
        self.emit("card")
        if not tr["queue"]:
            self.res = {"kind": "trick", "winner": self.players[tr["top_owner"]]["id"]}
            self.res_id += 1

    def draw_from(self, w):
        while self.deck and any(len(p["hand"]) < 4 for p in self.players):
            for k in range(self.n):
                p = self.players[(w + k) % self.n]
                if len(p["hand"]) < 4 and self.deck:
                    c = self.deck.pop()
                    p["hand"].append(c)
                    if c == self.trump_card:      # открытый козырь взяли: переворачиваем закрытую карту
                        self.trump_card = None
                        self.hidden_open = True
                        self.trump = st(self.hidden)
                        self.emit("trump")

    def resolve(self):
        if not self.res or self.over:
            return
        tr = self.trick
        w = tr["top_owner"]
        pile = [c for pl in tr["plays"] for c in pl["cards"]]
        self.pts[self.unit(w)] += sum(val(c) for c in pile)
        self.res = None
        self.trick = None
        self.emit("take")
        self.draw_from(w)
        self.emit("deal")
        self.next_leader = (w + 1) % self.n
        if all(not p["hand"] for p in self.players):
            self.end_deal()
        else:
            self.leader = self.next_leader
            self.log = f"Ходит {self.players[self.leader]['name']}"

    def end_deal(self):
        ffa = self.fmt == "ffa3"
        pts = list(self.pts)
        used = self.mult
        draw = not ffa and pts[0] == pts[1] == 60
        if draw:
            gain = [0, 0]
            self.mult *= 2
        else:
            gain = [penalty(p, ffa) * self.mult for p in pts]
            self.mult = 1
        for u in range(self.units):
            self.pen[u] += gain[u]
        self.board_id += 1
        self.board = {
            "deal": self.deal_no, "draw": draw, "used": used, "next_mult": self.mult,
            "rows": [{"name": self.unit_name(u), "pts": pts[u], "gain": gain[u], "pen": self.pen[u]}
                     for u in range(self.units)],
            "target": self.target,
        }
        reached = [u for u in range(self.units) if self.pen[u] >= self.target]
        if reached:
            worst = max(self.pen[u] for u in reached)
            losers = [u for u in reached if self.pen[u] == worst]
            self.loser_ids = [p["id"] for i, p in enumerate(self.players) if self.unit(i) in losers]
            self.board["final"] = [self.unit_name(u) for u in losers]
            self.over = True
        self.phase = "board"
        self.emit("score")

    def continue_(self):
        if self.phase != "board" or self.over:
            raise GameError("Сейчас нельзя")
        self.begin_deal(False)

    def surrender(self, uid):
        p = self.idx(uid)
        if p is None or self.over:
            raise GameError("Сейчас нельзя")
        self.over = True
        self.surrendered = True
        self.res = None
        self.loser_ids = [q["id"] for i, q in enumerate(self.players) if self.unit(i) == self.unit(p)]
        self.log = f"{self.players[p]['name']} сдался"

    # ------------------------------------------------------------ что видит игрок
    def turn_idx(self):
        if self.phase != "play" or self.res:
            return None
        if self.trick:
            return self.trick["queue"][0]
        return self.leader

    def view(self, uid):
        me = self.idx(uid)
        hand = []
        if me is not None:
            hand = sorted(self.players[me]["hand"], key=lambda c: (st(c) == self.trump, SUITS.index(st(c)), rk(c)))
        tr = self.trick
        turn = self.turn_idx()
        assign = {}
        for i in range(self.n):
            v = self.picks.get(self.unit(i))
            assign[self.players[i]["id"]] = v
        hidden = None
        if self.deck:
            if self.hidden_open and self.deck == [self.hidden]:
                hidden = self.hidden
            elif not self.hidden_open:
                hidden = "back"
        return {
            "kind": "bura",
            "fmt": self.fmt,
            "target": self.target,
            "phase": self.phase,
            "players": [
                {"id": p["id"], "name": p["name"], "n": len(p["hand"]), "team": self.unit(i) if self.fmt == "2x2" else None,
                 "turn": i == turn, "lead": self.phase == "play" and i == self.leader and not tr}
                for i, p in enumerate(self.players)
            ],
            "options": self.options(),
            "assign": assign,
            "leader_team": self.leader_team,
            "my_unit": self.unit(me) if me is not None else None,
            "trump": self.trump,
            "trump_card": self.trump_card,
            "hidden": hidden,
            "deck": len(self.deck),
            "trick": None if not tr else {
                "n": tr["n"],
                "plays": [{"p": self.players[pl["p"]]["id"], "mode": pl["mode"],
                           "cards": pl["cards"] if pl["mode"] != "pass" else [None] * len(pl["cards"])}
                          for pl in tr["plays"]],
                "top": self.players[tr["top_owner"]]["id"],
            },
            "turn": self.players[turn]["id"] if turn is not None else None,
            "hand": hand,
            "res": self.res,
            "board": self.board,
            "banner": self.banner,
            "log": self.log,
            "over": self.over,
            "loser_ids": self.loser_ids,
            "surrendered": self.surrendered,
        }
