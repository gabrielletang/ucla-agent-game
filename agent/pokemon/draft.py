"""Draft and Team Preview: code scores the options, the model makes the call."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from .calc import (Mon, NOTABLE_ABILITIES, SPREAD_TARGETS, data, estimate_damage, move, move_tags, species,
                   to_id, type_multiplier, weaknesses)

SPEED_CONTROL = {"tailwind", "trickroom", "icywind", "electroweb", "thunderwave"}
REDIRECT = {"followme", "ragepowder"}


def card_mon(card: dict) -> Mon:
    return Mon(card.get("species"), evs=card.get("evs"), ivs=card.get("ivs"), nature=card.get("nature"),
               item=card.get("item"), ability=card.get("ability"), moves=card.get("moves"), known_set=True,
               level=card.get("level") or 50)


def _attacking_moves(mon: Mon):
    for m in mon.moves:
        mv = move(m)
        if mv and mv.get("category") in ("Physical", "Special") and mv.get("bp"):
            yield m, mv


def profile(card: dict) -> dict:
    mon = card_mon(card)
    powers, coverage, cats = [], set(), {"Physical": 0, "Special": 0}
    for m, mv in _attacking_moves(mon):
        stat = mon.stat("atk" if mv["category"] == "Physical" else "spa")
        if m == "bodypress":
            stat = mon.stat("def")
        if mon.ability in ("hugepower", "purepower") and mv["category"] == "Physical":
            stat *= 2
        acc = mv.get("acc")
        acc = 1.0 if not isinstance(acc, (int, float)) or acc is True else acc / 100
        stab = 1.5 if mv["type"] in mon.types else 1.0
        spread = 1.15 if mv.get("target") in SPREAD_TARGETS else 1.0
        powers.append(mv["bp"] * stab * acc * spread * stat / 100)
        cats[mv["category"]] += 1
        for t in data()["typechart"]:
            if type_multiplier(mv["type"], [t]) > 1:
                coverage.add(t)
    powers.sort(reverse=True)
    offense = sum(powers[:2]) / 2 if powers else 0.0
    style = "physical" if cats["Physical"] > cats["Special"] else "special" if cats["Special"] else "support"
    if cats["Physical"] and cats["Special"] and abs(cats["Physical"] - cats["Special"]) <= 0:
        style = "mixed"
    tags = move_tags(mon.moves)
    if NOTABLE_ABILITIES.get(mon.ability):
        tags.append(NOTABLE_ABILITIES[mon.ability])
    return {
        "card_id": card.get("card_id"),
        "species": card.get("species"),
        "item": card.get("item"),
        "ability": card.get("ability"),
        "moves": card.get("moves"),
        "types": mon.types,
        "stats": mon.stats,
        "style": style,
        "offense": round(offense, 1),
        "bulk": round(mon.stats["hp"] * (mon.stats["def"] + mon.stats["spd"]) / 2 / 1000, 1),
        "speed": mon.stats["spe"],
        "coverage": sorted(coverage),
        "tags": tags,
        "weak_to": sorted(t for t, m in weaknesses(mon.types).items() if m > 1),
        "resists": sorted(t for t, m in weaknesses(mon.types).items() if m < 1),
        "_mon": mon,
    }


def _percentile(values, v) -> float:
    if not values:
        return 0.5
    return sum(1 for x in values if x <= v) / len(values)


def individual_scores(profiles: list[dict]) -> dict:
    off = [p["offense"] for p in profiles]
    bulk = [p["bulk"] for p in profiles]
    spe = [p["speed"] for p in profiles]
    out = {}
    for p in profiles:
        s = 0.45 * _percentile(off, p["offense"]) + 0.25 * _percentile(bulk, p["bulk"]) + 0.15 * _percentile(spe, p["speed"])
        moves = {to_id(m) for m in p["moves"] or []}
        abil = to_id(p["ability"])
        if "fakeout" in moves:
            s += 0.08
        if abil == "intimidate":
            s += 0.08
        if moves & SPEED_CONTROL:
            s += 0.08
        if moves & REDIRECT:
            s += 0.08
        if moves & {"spore", "sleeppowder"}:
            s += 0.07
        if moves & {"tailwind", "trickroom"} and abil == "prankster":
            s += 0.04
        if moves & {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap", "burningbulwark"}:
            s += 0.03
        if any(move(m) and move(m).get("target") in SPREAD_TARGETS and move(m).get("bp") for m in moves):
            s += 0.04
        out[p["card_id"]] = s
    return out


def synergy(p: dict, roster: list[dict]) -> tuple[float, list[str]]:
    """How much this card improves a roster (list of profiles). Returns (score, reasons)."""
    if not roster:
        return 0.0, []
    score, why = 0.0, []
    for t in data()["typechart"]:
        weak = sum(1 for r in roster if type_multiplier(t, r["types"]) > 1)
        if not weak:
            continue
        m = type_multiplier(t, p["types"])
        if m > 1:
            score -= 0.05 * weak
            if weak >= 2:
                why.append(f"stacks a {t} weakness")
        elif m < 1:
            score += 0.035 * weak
            if weak >= 2:
                why.append(f"covers team's {t} weakness")
    styles = [r["style"] for r in roster]
    phys, spec = styles.count("physical"), styles.count("special")
    if p["style"] == "physical" and phys < spec:
        score += 0.05
        why.append("balances toward physical")
    elif p["style"] == "special" and spec < phys:
        score += 0.05
        why.append("balances toward special")
    team_moves = {to_id(m) for r in roster for m in (r["moves"] or [])}
    moves = {to_id(m) for m in p["moves"] or []}
    if moves & SPEED_CONTROL and not team_moves & SPEED_CONTROL:
        score += 0.08
        why.append("adds speed control the team lacks")
    if "fakeout" in moves:
        score += 0.05 if "fakeout" not in team_moves else -0.02
    if to_id(p["ability"]) == "intimidate" and not any(to_id(r["ability"]) == "intimidate" for r in roster):
        score += 0.04
    new_cov = set(p["coverage"]) - {t for r in roster for t in r["coverage"]}
    if new_cov:
        score += 0.01 * len(new_cov)
        why.append("new coverage: " + ", ".join(sorted(new_cov)[:4]))
    return score, why


def rank_draft(obs: dict, my_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (ranked candidate profiles with scores, my roster profiles, opponent roster profiles)."""
    cards = {c["card_id"]: c for c in obs.get("available_cards") or [] if isinstance(c, dict) and c.get("card_id")}
    allowed = set(obs.get("available_card_ids") or cards)
    all_cards = dict(cards)
    pool_profiles = {cid: profile(c) for cid, c in all_cards.items()}
    rosters = obs.get("rosters") or {}
    mine_ids = [r.get("card_id") for r in rosters.get(my_id, []) if isinstance(r, dict)]
    opp_ids = [r.get("card_id") for k, v in rosters.items() if k != my_id for r in v if isinstance(r, dict)]
    known = obs.get("_known_cards", {})
    def prof(cid):
        if cid in pool_profiles:
            return pool_profiles[cid]
        if cid in known:
            return profile(known[cid])
        return None
    mine = [p for p in (prof(c) for c in mine_ids) if p]
    opp = [p for p in (prof(c) for c in opp_ids) if p]
    candidates = [pool_profiles[c] for c in pool_profiles if c in allowed]
    indiv = individual_scores(list(pool_profiles.values()))
    for p in candidates:
        syn, why = synergy(p, mine)
        deny, _ = synergy(p, opp)
        p["code_score"] = round(indiv.get(p["card_id"], 0.5) + syn + 0.35 * (indiv.get(p["card_id"], 0.5) + deny), 3)
        p["why"] = why
    candidates.sort(key=lambda p: p["code_score"], reverse=True)
    return candidates, mine, opp


def public(p: dict) -> dict:
    keys = ("card_id", "species", "item", "ability", "moves", "types", "stats", "style", "tags", "weak_to",
            "resists", "code_score", "why")
    return {k: p[k] for k in keys if k in p}


def seconds_until(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t - datetime.now(timezone.utc)).total_seconds()


# -- Team Preview ---------------------------------------------------------------------


def matchup_matrix(mine: list[Mon], theirs: list[Mon]) -> list[list[dict]]:
    """For each of my mons vs each foe: my best hit on it and its best hit on me (fractions of max HP)."""
    out = []
    for a in mine:
        row = []
        for d in theirs:
            mine_best = max((estimate_damage(a, d, m)["pct"] for m in a.moves), default=0.0)
            if not d.moves:  # unknown set: assume STAB 90 BP moves of its types, best category
                their_best = _assumed_threat(d, a)
            else:
                their_best = max((estimate_damage(d, a, m)["pct"] for m in d.moves), default=0.0)
            row.append({"deal": round(mine_best, 2), "take": round(their_best, 2),
                        "outspeeds": a.stat("spe") > d.stat("spe")})
        out.append(row)
    return out


def _assumed_threat(attacker: Mon, defender: Mon) -> float:
    best = 0.0
    for t in attacker.types:
        for mv in ("physical", "special"):
            fake = f"_{t}_{mv}"
            stat_a = attacker.stat("atk" if mv == "physical" else "spa")
            stat_d = defender.stat("def" if mv == "physical" else "spd")
            base = (22 * 90 * stat_a / max(1, stat_d)) / 50 + 2
            pct = base * 1.5 * type_multiplier(t, defender.types) * 0.925 / defender.max_hp
            best = max(best, pct)
    return best


def recommend_lineup(my_mons: list[Mon], opp_mons: list[Mon], my_profiles: list[dict]) -> tuple[list[int], list[int], list[float]]:
    matrix = matchup_matrix(my_mons, opp_mons)
    values = []
    for i, row in enumerate(matrix):
        v = 0.0
        for cell in row:
            v += min(cell["deal"], 1.2) - 0.7 * min(cell["take"], 1.2) + (0.08 if cell["outspeeds"] else 0)
        tags = my_profiles[i]["tags"] if i < len(my_profiles) else []
        v += 0.25 * sum(1 for t in tags if t in ("Fake Out (flinch turn 1)", "Intimidate", "speed control (Tailwind)",
                                                  "Trick Room", "redirection"))
        values.append(round(v / max(1, len(row)), 3))
    order = sorted(range(len(my_mons)), key=lambda i: values[i], reverse=True)
    bring = order[:4]
    best_pair, best_val = bring[:2], -1e9
    for a, b in itertools.combinations(bring, 2):
        v = values[a] + values[b]
        tags = set((my_profiles[a]["tags"] if a < len(my_profiles) else []) + (my_profiles[b]["tags"] if b < len(my_profiles) else []))
        if "Fake Out (flinch turn 1)" in tags:
            v += 0.15
        if tags & {"speed control (Tailwind)", "Trick Room", "redirection"}:
            v += 0.1
        if v > best_val:
            best_pair, best_val = [a, b], v
    return bring, best_pair, values
