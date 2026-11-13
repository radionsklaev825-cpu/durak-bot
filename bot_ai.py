"""Игровой бот для Дурака.

Бот не лезет во внутренности движка: он видит то же, что видит обычный игрок (eng.view(id)),
и ходит теми же командами (eng.act(id, {...})). Поэтому правила всегда соблюдаются движком.

Что умеет:
- Отбивается самой дешёвой подходящей картой, козыри бережёт.
- Если отбиться дорого (пришлось бы тратить козыри на мелочь), берёт карты.
- В переводном режиме переводит, когда это выгодно.
- Атакует мелкими некозырными картами, любит ходить парами одного достоинства.
- Подкидывает мусор, когда соперник берёт, и не подкидывает ценное.
- Считает карты: какие уже ушли в бито, какие соперник забрал себе (они у него в руке).
"""
import random

RANKS = ["6", "7", "8", "9", "10", "J", "Q", "K", "A"]
VAL = {r: i for i, r in enumerate(RANKS)}
SUITS = ["♠", "♥", "♦", "♣"]
ALL_CARDS = {r + s for r in RANKS for s in SUITS}


def rank(c):
    return c[:-1]


def suit(c):
    return c[-1]


def val(c):
    return VAL.get(rank(c), 0)


def beats(att, dfn, trump):
    """Бьёт ли карта dfn карту att."""
    if suit(dfn) == suit(att):
        return val(dfn) > val(att)
    return suit(dfn) == trump and suit(att) != trump


class DurakBot:
    # настройки силы игры (подобраны симуляциями)
    TAKE_JUNK = False      # брать мелочь, чтобы не тратить козырь
    TAKE_COST = 30         # брать, если отбиться стоит дороже этого (козыри весят +10)
    THROW_MAX = 8          # подкидывать некозыри достоинством не выше этого (индекс в RANKS, 8 = туз)
    THROW_TRUMP_END = 8    # в конце (колода пуста) подкидывать и козыри не выше этого достоинства; -1 = нет
    LEAD_HIGH_END = True   # в конце (колода пуста) заходить старшими некозырями

    def __init__(self):
        self.reset()

    def reset(self):
        self.discarded = set()   # карты, ушедшие в бито
        self.known = set()       # карты, которые соперник точно держит (забрал со стола)
        self._res_key = None
        self.blocked = None      # состояние, в котором движок отверг все наши варианты

    # ---------------------------------------------------------------- память
    def observe(self, G, me):
        table = self._table_cards(G)
        for c in table:
            self.known.discard(c)          # соперник сыграл эту карту: больше не «в руке»
        res = G.get("res")
        if res:
            key = (res.get("kind"), frozenset(table))
            if key != self._res_key:
                self._res_key = key
                if res.get("kind") == "bito":
                    self.discarded |= set(table)
                elif res.get("kind") == "take" and G.get("taker") != me:
                    self.known |= set(table)
        else:
            self._res_key = None

    @staticmethod
    def _table_cards(G):
        out = []
        for pr in G.get("table") or []:
            for c in pr:
                if c:
                    out.append(c)
        return out

    # ---------------------------------------------------------------- оценка
    @staticmethod
    def _value(c, trump):
        """Насколько карта ценна для самого бота: козыри намного ценнее."""
        return val(c) + (10 if suit(c) == trump else 0)

    def _opp_can_beat(self, c, trump):
        return any(beats(c, k, trump) for k in self.known)

    # ---------------------------------------------------------------- главное
    def decide(self, G, me):
        """Список команд по убыванию желательности. Пусто = сейчас ничего делать не нужно."""
        try:
            return self._decide(G, me)
        except Exception as e:      # бот не должен ронять игру
            print("bot decide error:", repr(e))
            return []

    def _decide(self, G, me):
        if G.get("over") or G.get("res"):
            return []
        players = G.get("players") or []
        mine = next((p for p in players if p.get("id") == me), None)
        if not mine or (mine.get("out") and "done" not in (G.get("actions") or [])):
            return []
        hand = list(G.get("hand") or [])
        if not hand and "done" not in (G.get("actions") or []):
            return []
        table = G.get("table") or []
        trump = G.get("trump")
        deck = G.get("deck") or 0
        legal = set(G.get("legal") or [])
        trn = set(G.get("tr") or [])
        actions = list(G.get("actions") or [])
        taking = bool(G.get("taking"))
        others = [p for p in players if p.get("id") != me and not p.get("out")]
        opp_n = min((p.get("n") or 0) for p in others) if others else 0
        unbeaten = [pr[0] for pr in table if not pr[1]]

        sig = (tuple(map(tuple, table)), tuple(sorted(hand)), taking)
        if self.blocked == sig:
            return []

        if mine.get("def"):
            cands = self._defend(hand, table, unbeaten, trump, deck, legal, trn, actions,
                                 taking, opp_n, bool(G.get("transfer")))
        else:
            cands = self._attack(hand, table, unbeaten, trump, deck, legal, actions, taking, opp_n)
        return cands

    # ---------------------------------------------------------------- защита
    def _plan(self, hand, unbeaten, trump, legal):
        """Чем бить каждую неотбитую карту: [(атака, защита)] или None, если отбиться нельзя."""
        order = sorted(unbeaten, key=lambda a: (suit(a) == trump, val(a)), reverse=True)
        used, plan = set(), []
        for a in order:
            opts = [c for c in hand if c not in used and beats(a, c, trump) and (not legal or c in legal)]
            if not opts:
                return None
            best = min(opts, key=lambda c: self._value(c, trump))
            used.add(best)
            plan.append((a, best))
        return plan

    def _defend(self, hand, table, unbeaten, trump, deck, legal, trn, actions, taking, opp_n, perevod):
        if taking or not unbeaten:
            return []                    # ждём соперника
        cands = []
        plan = self._plan(hand, unbeaten, trump, legal)
        # перевод (переводной режим): все карты не отбиты и есть чем перевести
        transfer = None
        if perevod and trn and all(not pr[1] for pr in table):
            pool = sorted(trn, key=lambda c: self._value(c, trump))
            transfer = pool[0]
        should_take = False
        if plan is not None:
            cost = sum(self._value(d, trump) for _a, d in plan)
            used_trump = sum(1 for _a, d in plan if suit(d) == trump)
            attack_junk = all(suit(a) != trump and val(a) <= 3 for a in unbeaten)
            late = deck <= 3
            if not late and len(hand) < 8:
                if self.TAKE_JUNK and used_trump and attack_junk and len(unbeaten) <= 2:
                    should_take = True       # не тратим козырь на мелочь
                elif cost >= self.TAKE_COST:
                    should_take = True       # слишком дорого
        if transfer and (plan is None or should_take
                         or (suit(transfer) != trump and val(transfer) <= 5 and opp_n > len(table))):
            cands.append({"t": "transfer", "card": transfer})
        if plan is not None and not should_take:
            for a, d in plan:
                cands.append({"t": "defend", "card": d, "target": a})
        if "take" in actions or plan is None or should_take:
            cands.append({"t": "take"})
        if plan is not None and should_take:           # запасной вариант, если брать нельзя
            for a, d in plan:
                cands.append({"t": "defend", "card": d, "target": a})
        return cands

    # ---------------------------------------------------------------- атака
    def _attack(self, hand, table, unbeaten, trump, deck, legal, actions, taking, opp_n):
        if not table:
            pool = [c for c in hand if not legal or c in legal]
            if not pool:
                return []
            return [{"t": "attack", "card": c} for c in self._lead_order(pool, hand, trump, deck)]
        if unbeaten and not taking:
            return []                    # соперник ещё отбивается: ждём
        pool = [c for c in hand if c in legal]
        throw = []
        for c in sorted(pool, key=lambda x: self._value(x, trump)):
            is_trump = suit(c) == trump
            if taking:
                # соперник берёт: сбрасываем лишнее, козыри только если это добивает руку
                if not is_trump or len(hand) <= len(throw) + 1:
                    throw.append(c)
            else:
                # соперник отбился: подкидываем только дешёвый мусор, пока есть что терять ему
                if not is_trump and val(c) <= self.THROW_MAX and opp_n > 0:
                    throw.append(c)
                elif is_trump and deck == 0 and opp_n > 0 and (len(hand) <= 2 or val(c) <= self.THROW_TRUMP_END):
                    throw.append(c)
        cands = [{"t": "attack", "card": c} for c in throw[:1]]
        if "done" in actions:
            cands.append({"t": "done"})
        return cands

    def _lead_order(self, pool, hand, trump, deck):
        """Какими картами ходить первой, от лучшей к худшей."""
        def score(c):
            s = float(self._value(c, trump))
            same = sum(1 for h in hand if rank(h) == rank(c))
            s -= 1.5 * (same - 1)                       # пары удобно подкидывать
            if self.known and not self._opp_can_beat(c, trump):
                s -= 2.5                                # соперник заведомо не побьёт дёшево
            if deck == 0 and suit(c) != trump:
                s += (-1.0 if self.LEAD_HIGH_END else 0.2) * val(c)
            return s + random.random() * 0.2            # чуть непредсказуемости
        return sorted(pool, key=score)


# ====================================================================== Бураказел
BURA_RANKS = ["6", "7", "8", "9", "J", "Q", "K", "10", "A"]
BURA_PTS = {"A": 11, "10": 10, "K": 4, "Q": 3, "J": 2}


def _bpts(c):
    return BURA_PTS.get(rank(c), 0)


def _bidx(c):
    return BURA_RANKS.index(rank(c)) if rank(c) in BURA_RANKS else 0


class BuraAuto:
    """Простой автоход для Бураказела: нужен, когда игрок молчит (таймер хода).

    Играет безопасно и без изысков: выбирает свободный цвет/масть, ходит самой дешёвой картой,
    бьёт одиночную карту самой дешёвой подходящей, всё остальное пасует младшими картами."""

    def reset(self):
        self.blocked = None

    def observe(self, G, me):
        pass

    def decide(self, G, me):
        try:
            return self._decide(G, me)
        except Exception as e:
            print("bura auto error:", repr(e))
            return []

    def _decide(self, G, me):
        if G.get("over") or G.get("res"):
            return []
        phase = G.get("phase")
        if phase == "pick":
            if (G.get("assign") or {}).get(me) is not None:
                return []
            return [{"t": "pick", "choice": o} for o in (G.get("options") or [])]
        if phase == "leader":
            if G.get("my_unit") == G.get("leader_team"):
                return []
            return [{"t": "leader", "target": p["id"]} for p in (G.get("players") or [])
                    if p.get("team") == G.get("leader_team")]
        if phase != "play" or G.get("turn") != me:
            return []
        hand = list(G.get("hand") or [])
        trump = G.get("trump")
        if not hand:
            return []
        trick = G.get("trick")

        def cheap(c):
            return (suit(c) == trump, _bpts(c), _bidx(c))
        if not trick:
            return [{"t": "lead", "cards": [c]} for c in sorted(hand, key=cheap)[:3]]
        n = trick.get("n") or 1
        cands = []
        plays = trick.get("plays") or []
        top_owner = trick.get("top")
        top = None
        for pl in plays:                      # карты, которые сейчас бить: последняя выигрышная выкладка
            if pl.get("p") == top_owner and pl.get("mode") in ("lead", "beat"):
                top = pl.get("cards")
        if n == 1 and top and top[0]:
            t0 = top[0]
            opts = [c for c in hand if (suit(c) == suit(t0) and _bidx(c) > _bidx(t0))
                    or (suit(c) == trump and suit(t0) != trump)]
            for c in sorted(opts, key=cheap)[:2]:
                cands.append({"t": "respond", "cards": [c], "mode": "beat"})
        low = sorted(hand, key=cheap)[:n]
        if len(low) == n:
            cands.append({"t": "respond", "cards": low, "mode": "pass"})
        return cands
