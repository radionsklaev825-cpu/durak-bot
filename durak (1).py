import random

RANKS = ["6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = ["♠", "♥", "♦", "♣"]


def rk(c):
    return RANKS.index(c[:-1])


def st(c):
    return c[-1]


class GameError(Exception):
    pass


class Durak:
    kind = "durak"

    def __init__(self, players, transfer=False, cheat=False, emit=None):
        self.emit = emit or (lambda *a, **k: None)
        self.transfer_mode = bool(transfer)
        self.cheat = bool(cheat)
        self.players = [{"id": p["id"], "name": p["name"], "hand": [], "out": False} for p in players]
        self.over = False
        self.loser = None
        self.surrendered = False
        self.res = None          # {"kind": "bito" | "take"}: пауза, чтобы все увидели стол
        self.res_id = 0
        self.bad = {}            # карта -> индекс игрока (нечестный ход)
        self.discard = 0
        self.table = []          # [[атака, защита | None], ...]
        self.taking = False
        self.passed = set()
        self.note = ""
        deck = [r + s for s in SUITS for r in RANKS]
        random.shuffle(deck)
        self.deck = deck
        self.trump = st(deck[0])
        for p in self.players:
            p["hand"] = [self.deck.pop() for _ in range(6)]
        best = None
        for i, p in enumerate(self.players):
            for c in p["hand"]:
                if st(c) == self.trump and (best is None or rk(c) < best[0]):
                    best = (rk(c), i)
        self.attacker = best[1] if best else random.randrange(len(self.players))
        self.defender = self.next_active(self.attacker)
        self.log = f"Ходит {self.players[self.attacker]['name']}"
        self.emit("deal")

    # ------------------------------------------------------------ помощники
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

    def room_ok(self):
        return len(self.table) < 6 and len(self.unbeaten()) + 1 <= len(self.players[self.defender]["hand"])

    def rank_ok(self, c):
        return rk(c) in {rk(x) for pair in self.table for x in pair if x}

    def throw_legal(self, p, c):
        if p == self.defender or self.players[p]["out"]:
            return False
        if not self.table:
            return p == self.attacker
        return self.room_ok() and self.rank_ok(c)

    def cheat_ok(self):
        # жульничать можно, пока защитник не нажал "Беру" и никто не нажал "Бито"
        return self.cheat and not self.taking and not self.passed

    def throw_allowed(self, p, c):
        if p == self.defender or self.players[p]["out"]:
            return False
        if not self.table:
            return p == self.attacker
        return self.room_ok() and (self.cheat_ok() or self.rank_ok(c))

    def transfer_ok(self, p, c):
        if not self.transfer_mode or p != self.defender or self.taking or not self.table:
            return False
        if any(t[1] for t in self.table):
            return False
        if len({rk(t[0]) for t in self.table}) != 1 or rk(self.table[0][0]) != rk(c):
            return False
        nxt = self.next_active(self.defender)
        return nxt != self.defender and len(self.players[nxt]["hand"]) >= len(self.table) + 1

    # ------------------------------------------------------------ действия
    def act(self, uid, m):
        t = m.get("t")
        p = self.idx(uid)
        if self.over:
            raise GameError("Игра окончена")
        if p is None:
            raise GameError("Вы не за столом")
        card = m.get("card")
        if t == "catch":
            return self.catch(p, card)
        if self.res:
            raise GameError("Подождите секунду")
        if t in ("attack", "defend", "transfer"):
            if not isinstance(card, str) or card not in self.players[p]["hand"]:
                raise GameError("Нет такой карты")
        if t == "attack":
            self.attack(p, card)
        elif t == "defend":
            tg = m.get("target")
            self.defend(p, card, tg if isinstance(tg, str) else None)
        elif t == "transfer":
            self.transfer(p, card)
        elif t == "take":
            self.take(p)
        elif t == "done":
            self.done(p)
        else:
            raise GameError("Неизвестное действие")

    def attack(self, p, card):
        if p == self.defender:
            raise GameError("Вы отбиваетесь")
        if not self.table and p != self.attacker:
            raise GameError("Сейчас ходит другой игрок")
        if not self.throw_allowed(p, card):
            raise GameError("Эту карту нельзя подкинуть")
        if self.table and not self.rank_ok(card):
            self.bad[card] = p
        self.players[p]["hand"].remove(card)
        self.table.append([card, None])
        self.passed = set()
        self.log = f"{self.players[p]['name']} ходит"
        self.emit("card")
        self.check_finish()

    def defend(self, p, card, target=None):
        if p != self.defender or self.taking:
            raise GameError("Сейчас нельзя отбиваться")
        pairs = self.unbeaten()
        if target:
            pairs = [t for t in pairs if t[0] == target]
        if not pairs:
            raise GameError("Нечего отбивать")
        chosen = next((t for t in pairs if self.beats(t[0], card)), None)
        if chosen is None:
            if not self.cheat:
                raise GameError("Этой картой не побить")
            chosen = pairs[0]
            self.bad[card] = p
        chosen[1] = card
        self.players[p]["hand"].remove(card)
        self.passed = set()
        self.log = f"{self.players[p]['name']} отбивается"
        self.emit("card")
        self.check_finish()

    def transfer(self, p, card):
        if not self.transfer_ok(p, card):
            raise GameError("Сейчас нельзя перевести")
        nxt = self.next_active(self.defender)
        self.players[p]["hand"].remove(card)
        self.table.append([card, None])
        self.attacker, self.defender = self.defender, nxt
        self.passed = set()
        self.log = f"{self.players[p]['name']} переводит на {self.players[nxt]['name']}"
        self.emit("card")

    def take(self, p):
        if p != self.defender or self.taking or not self.unbeaten():
            raise GameError("Сейчас нельзя взять карты")
        self.taking = True
        self.passed = set()
        self.log = f"{self.players[p]['name']} берёт карты, можно подкинуть"
        self.check_finish()

    def done(self, p):
        if p == self.defender or not self.table:
            raise GameError("Сейчас нельзя")
        if not self.taking and self.unbeaten():
            raise GameError("Дождитесь, пока игрок отобьётся")
        self.passed.add(p)
        self.check_finish()

    def catch(self, p, card):
        if not self.cheat:
            raise GameError("Шулерство выключено")
        if not isinstance(card, str) or not any(card in pair for pair in self.table):
            raise GameError("Этой карты нет на столе")
        owner = self.bad.get(card)
        if owner is None:
            raise GameError("Это честный ход")
        if owner == p:
            raise GameError("Это ваша карта")
        del self.bad[card]
        for pair in list(self.table):
            if pair[0] == card:
                self.table.remove(pair)
                self.players[owner]["hand"].append(card)
                if pair[1]:
                    self.bad.pop(pair[1], None)
                    self.players[self.defender]["hand"].append(pair[1])
                break
            if pair[1] == card:
                pair[1] = None
                self.players[owner]["hand"].append(card)
                break
        self.res = None
        self.res_id += 1
        self.passed = set()
        self.note = f"{self.players[p]['name']} поймал {self.players[owner]['name']} на шулерстве, карта вернулась"
        self.log = self.note
        self.emit("catch")

    def surrender(self, uid):
        p = self.idx(uid)
        if p is None or self.over:
            raise GameError("Сейчас нельзя")
        self.over = True
        self.surrendered = True
        self.loser = uid
        self.res = None
        self.log = f"{self.players[p]['name']} сдался"

    # ------------------------------------------------------------ конец хода
    def can_throw(self, i):
        pl = self.players[i]
        return i != self.defender and not pl["out"] and any(self.throw_allowed(i, c) for c in pl["hand"])

    def check_finish(self):
        if self.over or not self.table or self.res:
            return
        d = self.players[self.defender]
        if not self.taking:
            if self.unbeaten():
                return
            if len(self.table) >= 6 or not d["hand"]:
                return self.set_res(False)
        for i in range(len(self.players)):
            if i in self.passed:
                continue
            if self.can_throw(i):
                return
        self.set_res(self.taking)

    def set_res(self, taken):
        # в режиме шулерства стол остаётся на экране дольше: нужно время, чтобы поймать
        self.res = {"kind": "take" if taken else "bito", "delay": 5.0 if self.cheat else 2.4}
        self.res_id += 1
        self.log = "Берёт карты" if taken else "Бито"

    def draw_up(self):
        n = len(self.players)
        order = [(self.attacker + k) % n for k in range(n) if (self.attacker + k) % n != self.defender]
        order.append(self.defender)
        for j in order:
            hand = self.players[j]["hand"]
            while len(hand) < 6 and self.deck:
                hand.append(self.deck.pop())

    def resolve(self):
        if not self.res or self.over:
            return
        taken = self.res["kind"] == "take"
        d = self.players[self.defender]
        cards = [c for pair in self.table for c in pair if c]
        if taken:
            d["hand"].extend(cards)
            self.emit("take")
        else:
            self.discard += len(cards)
            self.emit("bito")
        self.res = None
        self.table, self.taking, self.passed = [], False, set()
        self.bad.clear()
        self.draw_up()
        self.emit("deal")
        for p in self.players:
            if not p["hand"] and not self.deck:
                p["out"] = True
        alive = [i for i, p in enumerate(self.players) if not p["out"]]
        if len(alive) <= 1:
            self.over = True
            self.loser = self.players[alive[0]]["id"] if alive else None
            return
        if taken or self.players[self.defender]["out"]:
            self.attacker = self.next_active(self.defender)
        else:
            self.attacker = self.defender
        self.defender = self.next_active(self.attacker)
        self.log = f"Ходит {self.players[self.attacker]['name']}"

    # ------------------------------------------------------------ что видит игрок
    def view(self, uid):
        me = self.idx(uid)
        hand, legal, tr, anyc, acts, hint = [], [], [], [], [], ""
        busy = bool(self.res) or self.over
        if me is not None:
            hand = sorted(self.players[me]["hand"], key=lambda c: (st(c) == self.trump, SUITS.index(st(c)), rk(c)))
            pl = self.players[me]
            if not busy and not pl["out"]:
                if me == self.defender:
                    if self.taking:
                        hint = "Вы берёте карты"
                    else:
                        un = [t[0] for t in self.unbeaten()]
                        legal = [c for c in hand if any(self.beats(a, c) for a in un)]
                        tr = [c for c in hand if self.transfer_ok(me, c)]
                        if un:
                            anyc = list(hand) if self.cheat else []
                            acts = ["take"]
                            hint = "Побейте карту, переведите или возьмите" if self.transfer_mode else "Побейте карту или возьмите"
                        else:
                            hint = "Ждём, пока подкинут"
                else:
                    legal = [c for c in hand if self.throw_legal(me, c)]
                    anyc = [c for c in hand if self.throw_allowed(me, c)] if self.cheat else []
                    if not self.table:
                        hint = "Ваш ход, атакуйте" if me == self.attacker else "Ходит другой игрок"
                    elif me not in self.passed and (self.taking or not self.unbeaten()):
                        acts = ["done"]
                        hint = "Подкиньте карту или завершите"
                    else:
                        hint = "Можно подкинуть" if legal else "Ждём остальных"
        return {
            "kind": "durak",
            "trump": self.trump,
            "trump_card": self.deck[0] if self.deck else None,
            "deck": len(self.deck),
            "discard": self.discard,
            "taking": self.taking,
            "cheat": self.cheat,
            "transfer": self.transfer_mode,
            "players": [
                {"id": p["id"], "name": p["name"], "n": len(p["hand"]), "out": p["out"],
                 "att": i == self.attacker and not self.over, "def": i == self.defender and not self.over,
                 "passed": i in self.passed}
                for i, p in enumerate(self.players)
            ],
            "table": self.table,
            "hand": hand,
            "legal": legal,
            "tr": tr,
            "any": anyc,
            "actions": acts,
            "hint": hint,
            "log": self.log,
            "res": self.res,
            "taker": self.players[self.defender]["id"] if self.res and self.res["kind"] == "take" else None,
            "over": self.over,
            "loser": self.loser,
            "surrendered": self.surrendered,
        }
