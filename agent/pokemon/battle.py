"""Doubles battle turns: annotate every legal option with numbers, then let the model choose."""

from __future__ import annotations

from .calc import Mon, SPREAD_TARGETS, estimate_damage, move, species, to_id, type_multiplier
from .draft import _assumed_threat

PROTECT = {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap", "burningbulwark", "obstruct"}


def _summary_mon(summary: dict, cards: dict[str, dict], *, own: bool) -> Mon | None:
    if not isinstance(summary, dict) or summary.get("fainted") or not summary.get("species"):
        return None
    sid = to_id(summary.get("species"))
    card = cards.get(sid)
    frac = summary.get("current_hp_fraction")
    if frac is None and summary.get("max_hp"):
        frac = (summary.get("current_hp") or 0) / max(1, summary["max_hp"])
    item = summary.get("item") or (card or {}).get("item")
    ability = summary.get("ability") or (card or {}).get("ability")
    moves = (card or {}).get("moves") or summary.get("moves") or []
    if isinstance(moves, dict):
        moves = list(moves)
    return Mon(summary.get("species"), types=summary.get("types"), base=summary.get("base_stats"),
               evs=(card or {}).get("evs"), ivs=(card or {}).get("ivs"), nature=(card or {}).get("nature"),
               item=item, ability=ability, moves=[m if isinstance(m, str) else (m or {}).get("id", "") for m in moves],
               hp_fraction=frac, status=summary.get("status"), boosts=summary.get("boosts"),
               known_set=bool(card), level=(card or {}).get("level") or summary.get("level") or 50)


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _flat(value) -> str:
    """Keys and values of a list/dict/str condition field, as one lowercase string."""
    if isinstance(value, dict):
        parts = [*map(str, value.keys()), *map(str, value.values())]
    elif isinstance(value, list):
        parts = [str(v) for v in value]
    else:
        parts = [str(value or "")]
    return " ".join(parts).lower()


def _field_flags(obs: dict) -> dict:
    fields = _flat(obs.get("fields"))
    mine = _flat(obs.get("side_conditions"))
    theirs = _flat(obs.get("opponent_side_conditions"))
    return {
        "weather": str(obs.get("weather") or ""),
        "trick_room": "trick" in fields,
        "my_tailwind": "tailwind" in mine,
        "their_tailwind": "tailwind" in theirs,
    }


def threat_on(defender: Mon, attackers: list[Mon | None], weather) -> float:
    best = 0.0
    for a in attackers:
        if a is None:
            continue
        if a.moves:
            dmg = max((estimate_damage(a, defender, m, weather=weather)["pct"] for m in a.moves), default=0.0)
        else:
            dmg = _assumed_threat(a, defender)
        best = max(best, dmg)
    return best


class TurnAnalysis:
    """Turns the server's doubles template + observation into annotated options and a code plan."""

    def __init__(self, template: dict, obs: dict, cards: dict[str, dict], memory: dict):
        self.template = template
        self.obs = obs
        self.flags = _field_flags(obs)
        actives = _as_list(obs.get("active_pokemon"))
        foes = _as_list(obs.get("opponent_active_pokemon"))
        self.my_raw = (actives + [None, None])[:2]
        self.foe_raw = (foes + [None, None])[:2]
        self.mine = [_summary_mon(s, cards, own=True) for s in self.my_raw]
        self.foes = [_summary_mon(s, cards, own=False) for s in self.foe_raw]
        self.memory = memory
        self.slots = sorted(template.get("slots") or [], key=lambda s: s.get("slot", 0))
        self.turn = obs.get("turn") or 0

    # -- per-option evaluation ------------------------------------------------------

    def _first_turn_out(self, slot: int) -> bool:
        mon = self.mine[slot]
        if mon is None:
            return False
        history = self.memory.setdefault("on_field", {})
        return history.get(slot, {}).get("species") != to_id(mon.name) or history.get(slot, {}).get("since") == self.turn

    def _target_mon(self, slot: int, target: int):
        if target in (1, 2):
            return "foe", self.foes[target - 1]
        if target in (-1, -2):
            idx = -target - 1
            return ("self" if idx == slot else "ally"), self.mine[idx]
        return "none", None

    def evaluate_move(self, slot: int, option: dict) -> dict:
        me = self.mine[slot]
        mid = to_id(option.get("move_id"))
        mv = move(mid) or {}
        info = {
            "move": mv.get("name") or option.get("move_id"),
            "type": mv.get("type") or option.get("move_type"),
            "category": mv.get("category") or option.get("category"),
            "base_power": mv.get("bp") if mv else option.get("base_power"),
            "priority": mv.get("pri", 0),
            "pp": option.get("current_pp"),
        }
        weather = self.flags["weather"]
        targets = option.get("targets") or []
        names = {t.get("target"): t for t in option.get("target_options") or [] if isinstance(t, dict)}
        best_value, best_target, rows = -1.0, (targets[0] if targets else None), []
        if me is None:
            return {**info, "value": 0.0, "best_target": best_target, "targets": rows}
        cat = mv.get("category")
        if cat in ("Physical", "Special") and mv.get("bp"):
            live_foes = [f for f in self.foes if f is not None]
            spread = mv.get("target") in SPREAD_TARGETS
            if spread or not targets:
                total, details = 0.0, []
                for i, f in enumerate(self.foes):
                    if f is None:
                        continue
                    d = estimate_damage(me, f, mid, spread_hits=max(1, len(live_foes)), weather=weather)
                    total += self._damage_value(d, f)
                    details.append({"is": f"OPPONENT {f.name}", "est_damage_pct_of_current_hp": self._pct_now(d, f),
                                    "likely_ko": self._ko(d, f), "effectiveness": d["eff"]})
                if mv.get("target") == "allAdjacent":
                    ally = self.mine[1 - slot]
                    if ally is not None:
                        d = estimate_damage(me, ally, mid, spread_hits=2, weather=weather)
                        total -= 1.2 * self._damage_value(d, ally)
                        details.append({"is": f"YOUR ALLY {ally.name} (also hit!)",
                                        "est_damage_pct_of_current_hp": self._pct_now(d, ally), "likely_ko": self._ko(d, ally)})
                rows.append({"target": targets[0] if targets else None, "hits": details})
                best_value = total
            else:
                for t in targets:
                    side, tm = self._target_mon(slot, t)
                    label = names.get(t, {}).get("species") or (tm.name if tm else "?")
                    if tm is None:
                        rows.append({"target": t, "is": f"{side.upper()} (empty)"})
                        continue
                    d = estimate_damage(me, tm, mid, weather=weather)
                    v = self._damage_value(d, tm)
                    if side != "foe":
                        v = -1.5 * v
                    rows.append({"target": t, "is": f"{'OPPONENT' if side == 'foe' else 'YOUR ' + side.upper()} {label}",
                                 "est_damage_pct_of_current_hp": self._pct_now(d, tm), "likely_ko": self._ko(d, tm),
                                 "effectiveness": d["eff"], **({"maybe_immune_by_ability": True} if d.get("maybe_immune") else {})})
                    if v > best_value:
                        best_value, best_target = v, t
            if mid == "fakeout":
                best_value = (best_value + 0.8) if self._first_turn_out(slot) else -1.0
                info["note"] = "Fake Out only works on this Pokémon's first turn out" + ("" if self._first_turn_out(slot) else " — it would FAIL now")
            elif info["priority"] > 0:
                best_value += 0.1
        else:
            best_value, note, best_target = self._status_value(slot, mid, mv, targets)
            info["note"] = note
            rows = [{"target": t, "is": self._label(slot, t, names)} for t in targets]
        return {**info, "value": round(best_value, 3), "best_target": best_target, "targets": rows}

    def _label(self, slot, t, names):
        side, tm = self._target_mon(slot, t)
        name = names.get(t, {}).get("species") or (tm.name if tm else "")
        return {"foe": f"OPPONENT {name}", "ally": f"YOUR ALLY {name}", "self": f"SELF {name}"}.get(side, "no target needed")

    def _pct_now(self, d: dict, target: Mon) -> int:
        return int(round(100 * d["pct"] / max(0.01, target.hp_fraction)))

    def _ko(self, d: dict, target: Mon) -> bool:
        return d["pct"] >= target.hp_fraction * 0.95

    def _damage_value(self, d: dict, target: Mon) -> float:
        if d.get("pct", 0) <= 0:
            return 0.0
        frac = max(0.01, target.hp_fraction)
        v = min(d["pct"], frac) / frac
        if self._ko(d, target):
            v += 0.6
        return v * d.get("acc", 1.0)

    def _status_value(self, slot: int, mid: str, mv: dict, targets: list) -> tuple[float, str, int | None]:
        me = self.mine[slot]
        threat = threat_on(me, self.foes, self.flags["weather"]) if me else 0.0
        last = self.memory.get("last_moves", {}).get(slot)
        tgt = next((t for t in targets if t in (1, 2) and self.foes[t - 1] is not None), targets[0] if targets else None)
        if mid in PROTECT:
            if last in PROTECT:
                return -0.5, "used Protect last turn: a second Protect in a row usually fails", tgt
            v = 0.7 if me and threat >= me.hp_fraction else 0.15
            return v, f"biggest expected hit on this Pokémon ≈{int(threat * 100)}% of max HP", tgt
        slower = self._we_are_slower()
        if mid == "tailwind":
            return (0.75 if not self.flags["my_tailwind"] and slower else 0.1), ("Tailwind already up" if self.flags["my_tailwind"] else "doubles our speed for 4 turns"), tgt
        if mid == "trickroom":
            if self.flags["trick_room"]:
                return -0.5, "Trick Room is up: using it again ENDS it", tgt
            return (0.8 if slower else -0.2), "reverses speed order for 5 turns", tgt
        if mid in ("icywind", "electroweb", "thunderwave"):
            return 0.4, "speed control", tgt
        if mid in ("followme", "ragepowder"):
            ally = self.mine[1 - slot]
            ally_threat = threat_on(ally, self.foes, self.flags["weather"]) if ally else 0
            return (0.5 if ally and ally_threat >= ally.hp_fraction * 0.8 else 0.25), "redirects single-target attacks to this Pokémon", tgt
        if mid == "helpinghand":
            return 0.35, "boosts ally's attack 1.5x this turn", tgt
        if mid in ("spore", "sleeppowder", "yawn", "willowisp", "thunderwave", "toxic", "glare", "nuzzle"):
            if tgt and tgt in (1, 2) and self.foes[tgt - 1] and self.foes[tgt - 1].status:
                return -0.3, "target already has a status condition", tgt
            return 0.45, "inflicts a status condition", tgt
        if mv.get("boosts") and mv.get("target") == "self":
            return (0.45 if me and me.hp_fraction > 0.6 and threat < 0.5 else 0.1), "stat boost", tgt
        if mv.get("heal") or mid in ("recover", "roost", "slackoff", "moonlight", "synthesis", "lifedew"):
            return (0.5 if me and me.hp_fraction < 0.5 else 0.05), "heals", tgt
        return 0.25, "status move", tgt

    def _we_are_slower(self) -> bool:
        mine = [m.stat("spe") for m in self.mine if m]
        theirs = [m.stat("spe") for m in self.foes if m]
        return bool(mine and theirs) and sum(mine) / len(mine) < sum(theirs) / len(theirs)

    def evaluate_switch(self, slot: int, option: dict, cards: dict) -> dict:
        sid = option.get("species")
        dex = species(sid) or {}
        card = cards.get(to_id(sid))
        incoming = Mon(sid, evs=(card or {}).get("evs"), nature=(card or {}).get("nature"), item=(card or {}).get("item"),
                       ability=(card or {}).get("ability"), moves=(card or {}).get("moves"), known_set=bool(card))
        bench = {to_id(b.get("species")): b for b in _as_list(self.obs.get("team")) if isinstance(b, dict)}
        frac = (bench.get(to_id(sid)) or {}).get("current_hp_fraction")
        if frac is not None:
            incoming.hp_fraction = float(frac)
        deal = max((estimate_damage(incoming, f, m)["pct"] for f in self.foes if f for m in incoming.moves), default=0.0)
        take = threat_on(incoming, self.foes, self.flags["weather"])
        forced = bool((_as_list(self.obs.get("force_switch")) + [False, False])[slot])
        value = min(deal, 1.0) - 0.8 * min(take, 1.0) + (0.4 if forced else -0.25)
        return {"switch_to": sid, "types": incoming.types, "hp_pct": int(incoming.hp_fraction * 100),
                "its_best_hit_on_foes_pct": int(deal * 100), "foes_best_hit_on_it_pct": int(take * 100),
                "value": round(value, 3), "best_target": None}

    # -- whole turn ------------------------------------------------------------------

    def annotate(self, cards: dict) -> list[list[dict]]:
        out = []
        for number, slot in enumerate(self.slots):
            rows = []
            for index, opt in enumerate(slot.get("options") or []):
                kind = opt.get("type")
                if kind == "move":
                    row = self.evaluate_move(number, opt)
                elif kind == "switch":
                    row = self.evaluate_switch(number, opt, cards)
                else:
                    row = {"value": -0.9 if len(slot.get("options") or []) > 1 else 0.0, "best_target": None}
                rows.append({"option": index, "kind": kind, **row})
            out.append(rows)
        return out

    def plan(self, annotated: list[list[dict]]) -> dict:
        """Best legal pair by code score (focus fire aware, no double switch into the same Pokémon)."""
        best, best_score = None, -1e9
        tops = [sorted(rows, key=lambda r: r["value"], reverse=True)[:6] for rows in annotated]
        while len(tops) < 2:
            tops.append([{"option": 0, "kind": "pass", "value": 0.0, "best_target": None}])
        for a in tops[0]:
            for b in tops[1]:
                if a["kind"] == b["kind"] == "switch" and a.get("switch_to") == b.get("switch_to"):
                    continue
                if a["kind"] == b["kind"] == "pass" and (len(annotated[0]) > 1 or len(annotated[1]) > 1):
                    continue
                score = a["value"] + b["value"]
                if a["kind"] == b["kind"] == "move" and a["best_target"] == b["best_target"] in (1, 2):
                    foe = self.foes[a["best_target"] - 1]
                    hit_a = self._hit_on(0, a, a["best_target"])
                    hit_b = self._hit_on(1, b, b["best_target"])
                    if foe and (hit_a or 0) >= foe.hp_fraction:
                        score -= 0.3  # overkill: slot 0 alone already KOs
                    elif foe and (hit_a or 0) + (hit_b or 0) >= foe.hp_fraction:
                        score += 0.4  # combined KO
                if score > best_score:
                    best, best_score = (a, b), score
        a, b = best
        return {"slot_0": {"option": a["option"], "target": a["best_target"]},
                "slot_1": {"option": b["option"], "target": b["best_target"]},
                "code_score": round(best_score, 3)}

    def _hit_on(self, slot: int, row: dict, target: int):
        for t in row.get("targets", []):
            if t.get("target") == target and "est_damage_pct_of_current_hp" in t:
                foe = self.foes[target - 1]
                return t["est_damage_pct_of_current_hp"] / 100 * (foe.hp_fraction if foe else 1)
        return None

    def board(self) -> dict:
        def side(mons, raws, own):
            out = []
            for m, raw in zip(mons, raws):
                if m is None:
                    out.append(None)
                    continue
                out.append({"species": m.name, "hp_pct": int(round(m.hp_fraction * 100)), "status": m.status,
                            "boosts": {k: v for k, v in m.boosts.items() if v}, "types": m.types,
                            "item": m.item or None, "ability": m.ability or None,
                            "speed_estimate": int(m.stat("spe")),
                            **({} if own else {"known_moves": m.moves[:4] or None})})
            return out
        return {
            "turn": self.turn,
            "field": self.flags,
            "my_active": side(self.mine, self.my_raw, True),
            "opponent_active": side(self.foes, self.foe_raw, False),
            "my_bench": [{"species": b.get("species"), "hp_pct": int(100 * (b.get("current_hp_fraction") or 0))}
                         for b in _as_list(self.obs.get("team")) if isinstance(b, dict) and not b.get("active") and not b.get("fainted")][:4],
            "opponent_revealed": [{"species": b.get("species"), "hp_pct": int(100 * (b.get("current_hp_fraction") or 0)), "fainted": bool(b.get("fainted"))}
                                  for b in _as_list(self.obs.get("opponent_team")) if isinstance(b, dict)][:6],
        }

    def remember(self, decision: dict) -> None:
        """Track first-turn-out (for Fake Out) and last moves (for Protect)."""
        on_field = self.memory.setdefault("on_field", {})
        last = self.memory.setdefault("last_moves", {})
        for slot in (0, 1):
            m = self.mine[slot]
            if m is None:
                continue
            sid = to_id(m.name)
            if on_field.get(slot, {}).get("species") != sid:
                on_field[slot] = {"species": sid, "since": self.turn}
            choice = decision.get(f"slot_{slot}") or {}
            last[slot] = to_id(choice.get("move_id")) if choice.get("type") == "move" else None
