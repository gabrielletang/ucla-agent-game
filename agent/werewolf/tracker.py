"""Werewolf bookkeeping: everything code can know for sure, so the model reasons over clean facts.

Tolerant of payload shapes: anything it cannot parse is still passed to the model raw.
"""

from __future__ import annotations

import re
from collections import defaultdict

ROLE_WORDS = {"werewolf": "wolf", "wolf": "wolf", "seer": "seer", "villager": "villager"}
SEER_CLAIM = re.compile(r"\b(i\s*am|i'm|im|as)\s+(the\s+|a\s+)?seer\b|\bseer\s+(here|claim)\b|\bi\s+(checked|investigated|inspected)\b", re.I)
SEAT = re.compile(r"\bplayer\s*(\d+)\b|\bseat\s*(\d+)\b|\bp(\d+)\b", re.I)


def _role_word(text: str) -> str | None:
    t = str(text or "").lower()
    for word, role in ROLE_WORDS.items():
        if word in t:
            return role
    return None


def seats_in(text: str) -> list[int]:
    out = []
    for m in SEAT.finditer(str(text or "")):
        n = next(g for g in m.groups() if g is not None)
        out.append(int(n))
    return out


class Tracker:
    def __init__(self):
        self.me: int | None = None
        self.names: dict[int, str] = {}
        self.pos_by_name: dict[str, int] = {}
        self.role: str | None = None
        self.allies: set[int] = set()
        self.checks: dict[int, str] = {}     # seer results: seat -> "wolf"/"villager"
        self.messages: list[dict] = []       # {"day", "from", "to", "text"}
        self.seen_message_ids: set[int] = set()
        self.claims: dict[int, list[str]] = defaultdict(list)
        self.accusations: dict[int, set[int]] = defaultdict(set)  # seer claimant -> seats they called wolf
        self.day = 0
        self.sub_phase = None
        self.alive: list[int] = []
        self.dead: list[dict] = []
        self.votes: list[dict] = []          # {"day", "voter", "target"} (target None = abstain)
        self.total_players = 0
        self.start_wolves = 2
        self.ally_proposals: list[dict] = [] # private messages from allies (wolves)

    # -- parsing ---------------------------------------------------------------------

    def seat(self, ref) -> int | None:
        if isinstance(ref, bool):
            return None
        if isinstance(ref, int):
            return ref
        if isinstance(ref, dict):
            for k in ("position", "seat", "player", "name"):
                if k in ref:
                    return self.seat(ref[k])
            return None
        s = str(ref or "")
        if s in self.pos_by_name:
            return self.pos_by_name[s]
        nums = seats_in(s)
        return nums[0] if nums else None

    def update(self, state) -> None:
        raw = state.raw
        self.me = raw.get("your_position", self.me)
        players = raw.get("players") or []
        if isinstance(players, dict):
            players = [{"position": int(k), "name": v} for k, v in players.items() if str(k).isdigit()]
        for p in players:
            if isinstance(p, dict) and isinstance(p.get("position"), int):
                self.names[p["position"]] = str(p.get("name") or f"Player{p['position']}")
                self.pos_by_name[self.names[p["position"]]] = p["position"]
                self.pos_by_name[f"Player{p['position']}"] = p["position"]
        self.total_players = max(self.total_players, len(self.names))
        self.start_wolves = 2 if self.total_players <= 11 else (3 if self.total_players <= 15 else 4)
        gs = raw.get("game_state") or {}
        self.day = gs.get("day") or self.day
        self.sub_phase = gs.get("phase")
        alive = [self.seat(x) for x in gs.get("alive") or []]
        if alive:
            self.alive = [a for a in alive if a is not None]
        dead = []
        for d in gs.get("dead") or []:
            if isinstance(d, dict):
                dead.append({"seat": self.seat(d.get("player")), "role": _role_word(d.get("role")) or d.get("role"),
                             "day": d.get("day"), "cause": d.get("cause")})
        if dead:
            self.dead = dead
        self._parse_votes(gs.get("vote_history"))
        self._parse_observation(state.observation if isinstance(state.observation, str) else str(raw.get("observation") or ""))
        for m in state.new_messages:
            if m.index in self.seen_message_ids:
                continue
            self.seen_message_ids.add(m.index)
            entry = {"day": self.day, "from": m.sender, "to": list(m.recipients) or "everyone", "text": m.content}
            self.messages.append(entry)
            text = m.content or ""
            if m.sender != self.me and SEER_CLAIM.search(text) and "seer" in text.lower():
                self.claims[m.sender].append(f"day {self.day}: claims seer")
            if m.sender != self.me and m.sender in self.claims and re.search(r"\bwolf\b|werewolf", text, re.I):
                named = [n for n in seats_in(text) if n != m.sender]
                if named:
                    self.accusations[m.sender].add(named[0])
            if m.recipients and m.sender in self.allies:
                self.ally_proposals.append(entry)

    def _parse_votes(self, history) -> None:
        if not history:
            return
        out = []
        def add(day, voter, target):
            v = self.seat(voter)
            t = None if target is None or "abstain" in str(target).lower() else self.seat(target)
            if v is not None:
                out.append({"day": day, "voter": v, "target": t})
        items = history.items() if isinstance(history, dict) else enumerate(history, start=1)
        for key, entry in items:
            day = key if not isinstance(entry, dict) else entry.get("day", key)
            if isinstance(entry, dict):
                votes = entry.get("votes") or entry.get("ballots") or {k: v for k, v in entry.items() if k not in ("day", "result", "eliminated", "tally")}
                if isinstance(votes, dict):
                    for voter, target in votes.items():
                        add(day, voter, target)
                elif isinstance(votes, list):
                    for b in votes:
                        if isinstance(b, dict):
                            add(day, b.get("voter") or b.get("from"), b.get("target") or b.get("vote"))
            elif isinstance(entry, list):
                for b in entry:
                    if isinstance(b, dict):
                        add(day, b.get("voter") or b.get("from"), b.get("target") or b.get("vote"))
        if out:
            self.votes = out

    def _parse_observation(self, text: str) -> None:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        header = []
        for l in lines:
            if re.search(r"\b(day|night)\s*\d|alive\s*\d+\s*/\s*\d+|public record|living", l, re.I) and header:
                break
            header.append(l)
        role_line = next((l for l in header if re.search(r"role", l, re.I)), None) or next(
            (l for l in lines if re.search(r"your role", l, re.I)), "")
        role = _role_word(re.sub(r".*role\s*[:=]?", "", role_line, flags=re.I)) if role_line else None
        if role:
            self.role = role
        for l in header:
            low = l.lower()
            nums = [n for n in seats_in(l) if n != self.me]
            if not nums:
                continue
            if self.role == "wolf" and re.search(r"wolf|wolves|ally|allies|pack|partner|teammate", low):
                self.allies.update(nums)
            if self.role == "seer" and re.search(r"investigat|check|result|inspect|saw", low) or (
                    self.role == "seer" and re.search(r"(is|:|=)\s*(a\s+)?(werewolf|wolf|villager|not a wolf)", low)):
                for n in nums:
                    seg = low.split(f"player{n}", 1)[-1] if f"player{n}" in low.replace(" ", "") else low
                    if "not a wolf" in seg or "villager" in seg or "innocent" in seg:
                        self.checks[n] = "villager"
                    elif "wolf" in seg:
                        self.checks[n] = "wolf"

    # -- derived facts -----------------------------------------------------------------

    def revealed(self) -> dict[int, str]:
        return {d["seat"]: d["role"] for d in self.dead if d.get("seat") is not None}

    def wolves_left(self) -> int:
        return max(0, self.start_wolves - sum(1 for r in self.revealed().values() if r == "wolf"))

    def parity(self) -> dict:
        alive = len(self.alive) or (self.total_players - len(self.dead))
        wolves = self.wolves_left()
        return {"alive": alive, "wolves_left": wolves, "village_left": alive - wolves,
                "wolves_win_if_village_loses": max(0, (alive - wolves) - wolves),
                "no_elimination_days_in_a_row": self._stall_days()}

    def _stall_days(self) -> int:
        days = sorted({v["day"] for v in self.votes if isinstance(v["day"], int)})
        vote_deaths = {d["day"] for d in self.dead if d.get("cause") == "vote"}
        streak = 0
        for day in reversed(days):
            if day in vote_deaths:
                break
            streak += 1
        return streak

    def suspicion(self) -> dict[int, float]:
        """Higher = more likely a wolf, from public evidence only (plus our own private knowledge)."""
        rev = self.revealed()
        score = {s: 0.0 for s in self.alive if s != self.me}
        for v in self.votes:
            if v["voter"] not in score or v["target"] is None:
                continue
            role = rev.get(v["target"])
            if role == "seer":
                score[v["voter"]] += 2.0
            elif role == "villager":
                score[v["voter"]] += 1.0
            elif role == "wolf":
                score[v["voter"]] -= 1.2
        # Voting alongside a revealed wolf is suspicious.
        for v in self.votes:
            if rev.get(v["voter"]) == "wolf" and v["target"] is not None:
                for w in self.votes:
                    if w["day"] == v["day"] and w["target"] == v["target"] and w["voter"] in score:
                        score[w["voter"]] += 0.5
        for s in score:
            if self.claims.get(s):
                score[s] -= 0.3  # a seer claim is evidence, but wolves can fake it
            if self.checks.get(s) == "wolf":
                score[s] += 10
            elif self.checks.get(s) == "villager":
                score[s] -= 10
            if s in self.allies:
                score[s] -= 20
        return score

    def facts(self) -> dict:
        rev = self.revealed()
        vote_lines = defaultdict(list)
        for v in self.votes:
            vote_lines[v["day"]].append(f"P{v['voter']}->{'abstain' if v['target'] is None else 'P' + str(v['target'])}")
        susp = self.suspicion()
        return {
            "key_deductions_from_code": self.deductions(),
            "me": self.me,
            "my_role": self.role,
            "my_wolf_allies": sorted(self.allies) if self.role == "wolf" else None,
            "my_seer_checks": {f"P{k}": v for k, v in self.checks.items()} if self.role == "seer" else None,
            "day": self.day,
            "sub_phase": self.sub_phase,
            "alive": [f"P{s}" for s in self.alive],
            "dead": [f"P{d['seat']} ({d['role']}, {d['cause']}, day {d['day']})" for d in self.dead],
            "votes_by_day": dict(vote_lines),
            "seer_claims": {f"P{k}": v for k, v in self.claims.items()},
            "seer_claimants_called_wolf": {f"P{k}": [f"P{x}" for x in sorted(v)] for k, v in self.accusations.items()},
            "parity": self.parity(),
            "code_suspicion_ranking": [f"P{s} ({round(v, 1)})" for s, v in sorted(susp.items(), key=lambda kv: -kv[1])],
            "votes_for_later_revealed_villagers": {f"P{s}": sum(1 for v in self.votes if v["voter"] == s and rev.get(v["target"]) in ("villager", "seer")) for s in self.alive if s != self.me},
        }

    def deductions(self) -> list[str]:
        """Plain-language conclusions that follow for certain from the public record."""
        rev = self.revealed()
        out = []
        for seat, role in rev.items():
            out.append(f"P{seat} is DEAD and was really a {str(role).upper()} (revealed). Dead players cannot be voted; "
                       f"do not discuss them as suspects.")
            if role == "seer":
                voters = sorted({v['voter'] for v in self.votes if v['target'] == seat and v['voter'] in self.alive})
                if voters:
                    out.append(f"P{seat} was the REAL seer, so their claims and checks were TRUE. Living players who voted "
                               f"to eliminate the real seer: {', '.join('P' + str(x) for x in voters)} — strong wolf suspects.")
                for s2, claims in self.claims.items():
                    if s2 != seat and s2 in self.alive and claims:
                        out.append(f"P{s2} also claimed seer, but the real seer was P{seat}: P{s2} LIED and is almost surely a wolf.")
        living_claims = [s2 for s2, c in self.claims.items() if c and s2 in self.alive]
        if len(living_claims) == 1 and not any(r == "seer" for r in rev.values()):
            out.append(f"P{living_claims[0]} is the ONLY seer claimant and nobody counter-claimed: the claim is probably TRUE. "
                       f"Do not vote them out; wolves benefit from killing the seer.")
        out.append(f"Wolves still alive: {self.wolves_left()} of {self.start_wolves}.")
        return out

    def transcript(self, limit: int = 40) -> list[dict]:
        return [{"day": m["day"], "from": f"P{m['from']}", "to": "everyone" if m["to"] == "everyone" else [f"P{r}" for r in m["to"]],
                 "text": m["text"]} for m in self.messages[-limit:]]


    # -- wolf helpers ---------------------------------------------------------------------

    def wolves_alive(self) -> list[int]:
        team = ({self.me} | self.allies) if self.me is not None else set(self.allies)
        return sorted(w for w in team if w in self.alive or not self.alive)

    def hostile_seer_claimants(self) -> list[int]:
        """Living non-wolf players claiming seer (for a wolf: the real seer or a bluffer, either way the target)."""
        return sorted(s for s, c in self.claims.items() if c and s in self.alive and s not in self.allies and s != self.me)

    def counter_claim_plan(self) -> dict | None:
        """If a seer claimant named one of us as a wolf, decide (deterministically, so both wolves agree) who counter-claims."""
        if self.role != "wolf":
            return None
        wolves = set(self.wolves_alive())
        for claimant in self.hostile_seer_claimants():
            accused = self.accusations.get(claimant, set()) & wolves
            if accused:
                unaccused = sorted(wolves - accused)
                who = unaccused[0] if unaccused else sorted(wolves)[0]
                return {"claimant": claimant, "accused": sorted(accused), "counter_claimer": who}
        return None
