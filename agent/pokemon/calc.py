"""Pokémon data lookups, stat calculation and a simplified Gen 9 damage estimate.

Data comes from Pokémon Showdown's gen 9 tables (MIT licensed, via poke-env),
compressed into data/gen9.json. The damage estimate is deliberately simple
(no crits, average roll, main items/abilities/weather) — it is used to rank
options for the model, not to replace Showdown's exact engine.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).with_name("data") / "gen9.json"
LEVEL = 50

STATS = ("hp", "atk", "def", "spa", "spd", "spe")
_STAT_ALIASES = {"hp": "hp", "atk": "atk", "attack": "atk", "def": "def", "defense": "def",
                 "spa": "spa", "spatk": "spa", "specialattack": "spa", "spd": "spd", "spdef": "spd",
                 "specialdefense": "spd", "spe": "spe", "speed": "spe"}

# Abilities that make the holder immune to a move type (possible-ability check for foes).
IMMUNITY_ABILITIES = {
    "levitate": "Ground", "earth eater": "Ground", "flash fire": "Fire", "well-baked body": "Fire",
    "water absorb": "Water", "storm drain": "Water", "dry skin": "Water", "volt absorb": "Electric",
    "lightning rod": "Electric", "motor drive": "Electric", "sap sipper": "Grass",
}

SUPPORT_MOVES = {
    "fakeout": "Fake Out (flinch turn 1)", "protect": "Protect", "detect": "Protect", "spikyshield": "Protect",
    "kingsshield": "Protect", "banefulbunker": "Protect", "silktrap": "Protect", "burningbulwark": "Protect",
    "tailwind": "speed control (Tailwind)", "trickroom": "Trick Room", "icywind": "speed control (Icy Wind)",
    "electroweb": "speed control (Electroweb)", "thunderwave": "speed control (paralysis)",
    "followme": "redirection", "ragepowder": "redirection", "helpinghand": "Helping Hand",
    "wideguard": "Wide Guard", "quickguard": "Quick Guard", "spore": "sleep", "sleeppowder": "sleep",
    "willowisp": "burn", "partingshot": "pivot", "uturn": "pivot", "voltswitch": "pivot",
    "snarl": "special attack drop", "taunt": "Taunt", "encore": "Encore", "reflect": "screens",
    "lightscreen": "screens", "auroraveil": "screens", "pollenpuff": "heal ally", "lifedew": "heal",
    "recover": "heal", "roost": "heal", "swordsdance": "setup", "nastyplot": "setup", "calmmind": "setup",
    "dragondance": "setup", "bulkup": "setup", "quiverdance": "setup", "bellydrum": "setup",
    "shedtail": "pivot", "afteryou": "support", "allyswitch": "support", "imprison": "support",
}
NOTABLE_ABILITIES = {
    "intimidate": "Intimidate", "drought": "sun setter", "drizzle": "rain setter", "sandstream": "sand setter",
    "snowwarning": "snow setter", "electricsurge": "terrain setter", "psychicsurge": "terrain setter",
    "grassysurge": "terrain setter", "mistysurge": "terrain setter", "prankster": "Prankster",
    "friendguard": "Friend Guard", "innerfocus": "Fake Out immune", "levitate": "Ground immune",
    "regenerator": "Regenerator", "unaware": "Unaware", "multiscale": "Multiscale",
    "goodasgold": "status immune", "armortail": "blocks priority", "queenlymajesty": "blocks priority",
    "dazzling": "blocks priority", "telepathy": "ally-hit immune", "hugepower": "Huge Power",
    "purepower": "Huge Power", "adaptability": "Adaptability", "protean": "Protean", "libero": "Protean",
    "commander": "Commander", "orichalcumpulse": "sun setter", "hadronengine": "terrain setter",
    "beadsofruin": "Ruin aura", "swordofruin": "Ruin aura", "tabletsofruin": "Ruin aura",
    "vesselofruin": "Ruin aura", "unseenfist": "ignores Protect", "sharpness": "Sharpness",
    "toughclaws": "Tough Claws", "technician": "Technician", "sheerforce": "Sheer Force",
}

SPREAD_TARGETS = {"allAdjacentFoes", "allAdjacent"}


def to_id(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


@lru_cache(maxsize=1)
def data() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def species(name) -> dict | None:
    dex = data()["pokedex"]
    key = to_id(name)
    if key in dex:
        return dex[key]
    # Forms like "Ogerpon-Wellspring-Mask" or "Urshifu-*": try progressively shorter ids.
    parts = re.split(r"[-\s]", str(name or ""))
    while len(parts) > 1:
        parts = parts[:-1]
        key = to_id("".join(parts))
        if key in dex:
            return dex[key]
    return None


def move(name) -> dict | None:
    return data()["moves"].get(to_id(name))


def type_multiplier(attack_type: str, defender_types) -> float:
    chart = data()["typechart"]
    mult = 1.0
    for t in defender_types or []:
        t = str(t).capitalize()
        mult *= chart.get(t, {}).get(str(attack_type).capitalize(), 1.0)
    return mult


def norm_stats(raw) -> dict:
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = _STAT_ALIASES.get(to_id(k))
            if key and isinstance(v, (int, float)):
                out[key] = v
    return out


def calc_stats(base: dict, evs: dict | None = None, ivs: dict | None = None, nature: str | None = None,
               level: int = LEVEL) -> dict:
    evs, ivs = norm_stats(evs or {}), norm_stats(ivs or {})
    nat = data()["natures"].get(to_id(nature), {}) if nature else {}
    out = {}
    for s in STATS:
        b = base.get(s, 80)
        iv = ivs.get(s, 31)
        ev = evs.get(s, 0)
        core = math.floor((2 * b + iv + math.floor(ev / 4)) * level / 100)
        if s == "hp":
            out[s] = 1 if b == 1 else core + level + 10
        else:
            out[s] = math.floor((core + 5) * nat.get(s, 1.0))
    return out


# Typical spread assumed for a foe whose set we don't know.
DEFAULT_FOE_EVS = {"hp": 124, "atk": 124, "spa": 124, "def": 60, "spd": 60, "spe": 116}


class Mon:
    """Everything the calculator needs about one Pokémon (own or foe)."""

    def __init__(self, name, *, types=None, base=None, evs=None, ivs=None, nature=None, item=None,
                 ability=None, moves=None, hp_fraction=1.0, status=None, boosts=None, known_set=False,
                 level=LEVEL):
        dex = species(name) or {}
        self.name = str(name)
        self.types = [str(t).capitalize() for t in (types or dex.get("types") or ["Normal"])]
        self.base = norm_stats(base) or dex.get("base") or {s: 80 for s in STATS}
        self.known_set = known_set
        self.stats = calc_stats(self.base, evs if evs else (None if known_set else DEFAULT_FOE_EVS), ivs, nature, level)
        self.item = to_id(item) if item else ""
        self.ability = to_id(ability) if ability else ""
        self.possible_abilities = [to_id(a) for a in dex.get("abilities", [])] if not ability else [to_id(ability)]
        self.moves = [to_id(m) for m in (moves or [])]
        self.hp_fraction = 1.0 if hp_fraction is None else max(0.0, min(1.0, float(hp_fraction)))
        self.status = (status or "").lower() or None
        self.boosts = {k: int(v) for k, v in (boosts or {}).items() if isinstance(v, (int, float))}
        self.level = level

    def stat(self, s: str) -> float:
        value = self.stats.get(s, 100)
        stage = self.boosts.get(s, 0)
        if stage:
            value *= (2 + stage) / 2 if stage > 0 else 2 / (2 - stage)
        if s == "spe":
            if self.item == "choicescarf":
                value *= 1.5
            if self.status == "par":
                value *= 0.5
        return value

    @property
    def max_hp(self) -> int:
        return self.stats["hp"]


def _immune_by_ability(mtype: str, defender: Mon) -> float:
    """1.0 = surely immune, 0.0 = surely not, in between = some possible abilities grant immunity."""
    abil = defender.possible_abilities
    if not abil:
        return 0.0
    hits = sum(1 for a in abil if any(to_id(k) == a and v == mtype for k, v in IMMUNITY_ABILITIES.items()))
    return hits / len(abil)


def estimate_damage(attacker: Mon, defender: Mon, move_name, *, spread_hits: int = 1, weather: str | None = None,
                    helping_hand=False) -> dict:
    """Average damage as a fraction of the defender's max HP, plus effectiveness info."""
    mv = move(move_name)
    if not mv or mv.get("category") == "Status" or not mv.get("bp"):
        return {"pct": 0.0, "eff": 1.0, "status": True}
    mtype = mv["type"]
    eff = type_multiplier(mtype, defender.types)
    # Common type-changing / special cases kept minimal on purpose.
    if mtype == "Ground" and defender.item == "airballoon":
        eff = 0.0
    immune_share = _immune_by_ability(mtype, defender)
    if eff == 0.0:
        return {"pct": 0.0, "eff": 0.0, "status": False}
    physical = mv["category"] == "Physical"
    a_stat = attacker.stat("atk" if physical else "spa")
    if to_id(move_name) == "bodypress":
        a_stat = attacker.stat("def")
    d_stat = defender.stat("def" if physical else "spd")
    if to_id(move_name) in ("psyshock", "psystrike", "secretsword"):
        d_stat = defender.stat("def")
    if physical and attacker.ability in ("hugepower", "purepower"):
        a_stat *= 2
    if attacker.item == ("choiceband" if physical else "choicespecs"):
        a_stat *= 1.5
    if not physical and defender.item == "assaultvest":
        d_stat *= 1.5
    bp = mv["bp"]
    hits = mv.get("multihit")
    if isinstance(hits, list):
        bp *= (hits[0] + hits[-1]) / 2 if attacker.ability != "skilllink" else hits[-1]
    elif isinstance(hits, int):
        bp *= hits
    if attacker.ability == "technician" and mv["bp"] <= 60:
        bp *= 1.5
    if attacker.ability == "sheerforce" and mv.get("secondary"):
        bp *= 1.3
    if attacker.ability == "toughclaws" and mv.get("contact"):
        bp *= 1.3
    base = math.floor(math.floor(math.floor(2 * attacker.level / 5 + 2) * bp * a_stat / max(1, d_stat)) / 50) + 2
    mod = 1.0
    if spread_hits > 1 and mv.get("target") in SPREAD_TARGETS:
        mod *= 0.75
    w = (weather or "").lower()
    if ("sun" in w or "desolate" in w) and mtype in ("Fire", "Water"):
        mod *= 1.5 if mtype == "Fire" else 0.5
    if ("rain" in w or "primordial" in w) and mtype in ("Fire", "Water"):
        mod *= 1.5 if mtype == "Water" else 0.5
    if mtype in attacker.types:
        mod *= 2.0 if attacker.ability == "adaptability" else 1.5
    mod *= eff
    if physical and attacker.status == "brn" and attacker.ability != "guts":
        mod *= 0.5
    if attacker.item == "lifeorb":
        mod *= 1.3
    elif attacker.item == "expertbelt" and eff > 1:
        mod *= 1.2
    if helping_hand:
        mod *= 1.5
    if defender.ability == "multiscale" and defender.hp_fraction >= 0.999:
        mod *= 0.5
    acc = mv.get("acc")
    acc = 1.0 if acc is True or not isinstance(acc, (int, float)) else acc / 100
    dmg = base * mod * 0.925  # average roll
    pct = dmg / max(1, defender.max_hp) * (1 - immune_share)
    return {"pct": round(pct, 3), "eff": eff, "acc": acc, "status": False, "maybe_immune": immune_share > 0}


def speed_order(a: Mon, b: Mon, trick_room=False, tailwind_a=False, tailwind_b=False) -> str:
    sa = a.stat("spe") * (2 if tailwind_a else 1)
    sb = b.stat("spe") * (2 if tailwind_b else 1)
    if abs(sa - sb) < 1:
        return "tie"
    faster = sa > sb
    if trick_room:
        faster = not faster
    return "faster" if faster else "slower"


def weaknesses(types) -> dict:
    """attack type -> multiplier for every type that hits this typing super-effectively or not at all."""
    out = {}
    for t in data()["typechart"]:
        m = type_multiplier(t, types)
        if m != 1.0:
            out[t] = m
    return out


def move_tags(move_ids) -> list[str]:
    tags = []
    for m in move_ids:
        tag = SUPPORT_MOVES.get(to_id(m))
        if tag and tag not in tags:
            tags.append(tag)
    return tags
