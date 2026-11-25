"""Pokémon VGC doubles draft player: code computes, the LLM decides.

Every decision goes through the model with a code-computed shortlist and a
recommended answer. The model can follow or overrule the recommendation; if it
fails or runs out of time, the code recommendation is played.
"""

from __future__ import annotations

import time

from altruagent import GameState, LegalAction, WithReasoning
from examples import smoke_agent
from examples.llm import pokemon as starter_pokemon

from ..llm import Brain, object_schema
from .battle import TurnAnalysis
from .calc import Mon, to_id
from .draft import card_mon, matchup_matrix, profile, public, rank_draft, recommend_lineup, seconds_until

BASE = """You are a strong Pokémon VGC player (Gen 9 doubles, level 50, no Terastallization) controlling one side of a
draft battle. Each request is one decision. The server's options are authoritative: pick only from them.
Our code pre-computes numbers for you (stats, type matchups, estimated damage as % of the target's CURRENT HP,
likely KOs, speed estimates) and a baseline choice. Treat the numbers as good estimates, not certainties.
Only state facts that are in the payload; do not invent moves, abilities or items.
reasoning_summary is PUBLIC (the opponent may read it): one short sentence, no secret plans."""

DRAFT_SYSTEM = BASE + """
This is TEAM BUILDING (draft / team preview), not a battle turn. Write `analysis` FIRST (max 50 words): what team plan
you are building (e.g. Trick Room, Tailwind offense, weather, bulky balance), what the opponent seems to be building,
and why your choice fits. code_score is a rough heuristic; agree with the baseline only when your plan lands there."""

SYSTEM = BASE + """
This is a BATTLE TURN. The code's numbers and baseline IGNORE what the opponent will do; that is your job.
Predict the opponent's most likely moves (Protect on a threatened slot, switching out of a bad matchup, priority moves,
Fake Out ONLY if `can_use_fake_out_this_turn` is true, focus fire on your weaker slot) and choose the line that does best
against that prediction. Focus fire to remove threats, respect speed order, never hit your own ally unless it is immune.
Write `analysis` FIRST (max 60 words: the opponent's likely moves, then your plan), then the decision.
Each slot has a menu of choice ids ("option:target"), each described in words with the code's numbers. Copy one id per slot.
Make sure the choice you pick matches what your analysis says."""

DRAFT_SHORTLIST = 6
SUPPORT_TAGS = {"redirection", "speed control (Tailwind)", "Trick Room", "sleep", "Intimidate", "Fake Out (flinch turn 1)"}
DRAFT_MAX_SECONDS = 9.0
PREVIEW_MAX_SECONDS = 30.0
TURN_MAX_SECONDS = 20.0
TURN_SAFETY_SECONDS = 10.0


class PokemonPlayer:
    def __init__(self, brain: Brain, log=print):
        self.brain = brain
        self.log = log
        self.cards: dict[str, dict] = {}          # card_id -> card (every card ever seen in the pool)
        self.by_species: dict[str, dict] = {}     # species id -> card
        self.my_id: str | None = None
        self.battle_memory: dict = {}
        self.bank_spent = 0.0

    # -- entry point -----------------------------------------------------------------

    def choose_action(self, state: GameState, context):
        obs = state.raw.get("observation") if isinstance(state.raw.get("observation"), dict) else {}
        first = state.legal_actions[0] if state.legal_actions else None
        if first is None:
            raise ValueError("no legal actions")
        if first.action_id == "select_lineup":
            return self._lineup(first, state, obs)
        if first.action_id == "doubles_turn":
            return self._turn(first, state, obs)
        if state.phase == "draft" or first.action_id.startswith("draft_pick:"):
            return self._draft(state, obs)
        return smoke_agent.choose_action(state, None)

    # -- draft -----------------------------------------------------------------------

    def _remember_cards(self, obs: dict) -> None:
        for c in obs.get("available_cards") or []:
            if isinstance(c, dict) and c.get("card_id"):
                self.cards[c["card_id"]] = c
                self.by_species[to_id(c.get("species"))] = c

    def _draft(self, state: GameState, obs: dict):
        started = time.monotonic()
        self._remember_cards(obs)
        if obs.get("current_seat"):
            self.my_id = obs["current_seat"]
        by_card = {a.action_id.split(":", 1)[1]: a for a in state.legal_actions if a.action_id.startswith("draft_pick:")}
        obs = {**obs, "available_card_ids": [c for c in (obs.get("available_card_ids") or by_card) if c in by_card],
               "_known_cards": self.cards}
        ranked, mine, opp = rank_draft(obs, self.my_id or "")
        ranked = [p for p in ranked if p["card_id"] in by_card]
        if not ranked:
            return state.legal_actions[0]
        shortlist = ranked[:DRAFT_SHORTLIST]
        support = next((p for p in ranked[DRAFT_SHORTLIST:] if set(p["tags"]) & SUPPORT_TAGS), None)
        if support and not any(set(p["tags"]) & SUPPORT_TAGS for p in shortlist):
            shortlist = shortlist + [support]
        fallback = by_card[shortlist[0]["card_id"]]
        left = seconds_until(obs.get("decision_deadline_at"))
        budget = DRAFT_MAX_SECONDS if left is None else min(DRAFT_MAX_SECONDS, left - 3.0 - (time.monotonic() - started))
        payload = {
            "decision": "draft_pick",
            "how_it_works": "Snake draft, 6 picks each from a shared pool; you will bring 4 of your 6 to a doubles battle.",
            "picks_remaining": obs.get("picks_remaining"),
            "my_roster": [public(p) for p in mine],
            "opponent_roster": [public(p) for p in opp],
            "candidates_ranked_by_code": [public(p) for p in shortlist],
            "code_baseline": shortlist[0]["card_id"],
            "guidance": "code_score is a rough single-number heuristic. Decide with a TEAM plan in mind (e.g. Trick Room, "
                        "Tailwind offense, weather, balance): cover shared weaknesses, mix physical and special, keep speed "
                        "control and Fake Out/Intimidate support, and consider denying a card the opponent clearly needs.",
        }
        ids = [p["card_id"] for p in shortlist]

        def build(ans):
            cid = ans.get("card_id")
            if cid not in by_card:
                raise ValueError(f"card_id must be one of {ids}")
            return by_card[cid]

        if budget < 2.5:
            self.log("[pokemon] draft: no time for the model, playing code pick")
            return WithReasoning(fallback, "Best available fit for the team.")
        value, ans = self.brain.ask(kind="draft_pick", system=DRAFT_SYSTEM, payload=payload,
                                    schema=object_schema({"analysis": {"type": "string"},
                                                          "card_id": {"type": "string", "enum": ids},
                                                          "reasoning_summary": {"type": "string"}}),
                                    build=build, budget=budget)
        if value is None:
            self.log(f"[pokemon] draft: FALLBACK to code pick {shortlist[0]['species']}")
            return WithReasoning(fallback, "Best available fit for the team.")
        pick = next(p for p in shortlist if p["card_id"] == value.action_id.split(":", 1)[1])
        self.log(f"[pokemon] draft: picked {pick['species']} (code #1 was {shortlist[0]['species']}) | {ans.get('analysis', '')}")
        return WithReasoning(value, str(ans.get("reasoning_summary") or "")[:200])

    # -- team preview ----------------------------------------------------------------

    def _mon_for(self, entry) -> tuple[str, Mon]:
        name = entry if isinstance(entry, str) else (entry.get("species") or entry.get("name"))
        card = self.by_species.get(to_id(name))
        if card:
            return name, card_mon(card)
        types = entry.get("types") if isinstance(entry, dict) else None
        base = entry.get("base_stats") if isinstance(entry, dict) else None
        return name, Mon(name, types=types, base=base if base and "…" not in base else None)

    def _lineup(self, action: LegalAction, state: GameState, obs: dict):
        choice = starter_pokemon.lineup_choice(action, state)  # validated builder + safe fallback
        roster = list(dict.fromkeys(action.input["action"]["roster"]))
        mine = [self._mon_for(next((e for e in obs.get("your_roster") or [] if isinstance(e, dict)
                                    and to_id(e.get("species")) == to_id(s)), s))[1] for s in roster]
        opp_entries = obs.get("opponent_roster") or []
        opp = [self._mon_for(e) for e in opp_entries]
        opp_names, opp_mons = [n for n, _ in opp], [m for _, m in opp]
        my_profiles = [profile(self.by_species[to_id(s)]) if to_id(s) in self.by_species else
                       {"tags": [], "species": s} for s in roster]
        try:
            bring_i, lead_i, values = recommend_lineup(mine, opp_mons, my_profiles)
            rec = {"type": "select_lineup", "bring": [roster[i] for i in bring_i], "leads": [roster[i] for i in lead_i]}
            choice.build(rec)
        except Exception as exc:  # never let analysis break the decision
            self.log(f"[pokemon] lineup analysis failed: {exc}")
            rec, values = choice.fallback(), [0] * len(roster)
        matrix = matchup_matrix(mine, opp_mons) if opp_mons else []
        payload = {
            "decision": "select_lineup",
            "rules": "Choose 4 of your 6 to bring and 2 of those 4 to lead.",
            "my_roster": [{"species": s, **{k: v for k, v in public(p).items() if k in ("item", "ability", "moves", "types", "stats", "tags")},
                           "code_value_vs_their_team": values[i]} for i, (s, p) in enumerate(zip(roster, my_profiles))],
            "opponent_roster": [{"species": n, "types": m.types, "speed_estimate": int(m.stat("spe")),
                                 **({"item": self.by_species[to_id(n)].get("item"), "ability": self.by_species[to_id(n)].get("ability"),
                                     "moves": self.by_species[to_id(n)].get("moves")} if to_id(n) in self.by_species else {})}
                                for n, m in zip(opp_names, opp_mons)],
            "matchups": "matrix[my_i][their_j] = {deal: my best hit on them, take: their best hit on me (fraction of max HP), outspeeds}",
            "matrix": matrix,
            "code_baseline": rec,
            "guidance": "Think about which 2 of their Pokémon they are most likely to lead with and pick leads that beat that.",
        }
        schema_species = {"type": "array", "items": {"type": "string", "enum": roster}}
        value, ans = self.brain.ask(kind="select_lineup", system=DRAFT_SYSTEM, payload=payload,
                                    schema=object_schema({"analysis": {"type": "string"}, "bring": schema_species, "leads": schema_species,
                                                          "reasoning_summary": {"type": "string"}}),
                                    build=choice.build, budget=PREVIEW_MAX_SECONDS)
        if value is None:
            self.log(f"[pokemon] lineup: FALLBACK to code lineup {rec}")
            return WithReasoning(rec, "Lineup chosen for the matchup.")
        self.log(f"[pokemon] lineup: bring {value['bring']} lead {value['leads']} (code: {rec['bring']} / {rec['leads']}) | {ans.get('analysis', '')}")
        return WithReasoning(value, str(ans.get("reasoning_summary") or "")[:200])

    # -- battle turns ----------------------------------------------------------------

    def _turn(self, action: LegalAction, state: GameState, obs: dict):
        started = time.monotonic()
        choice = starter_pokemon.doubles_choice(action, state)  # validated builder + safe fallback
        template = action.input["action"]
        cards = self.by_species
        try:
            analysis = TurnAnalysis(template, obs, cards, self.battle_memory)
            annotated = analysis.annotate(cards)
            plan = analysis.plan(annotated)
            plan_action = choice.build(plan)
        except Exception as exc:
            self.log(f"[pokemon] turn analysis failed ({exc}); using starter fallback")
            return WithReasoning(choice.fallback(), "Default move.")

        clock = obs.get("clock") or {}
        decision_left = clock.get("decision_seconds_left") or 55
        bank_left = clock.get("bank_seconds_left") or max(30.0, 420 - self.bank_spent)
        budget = min(TURN_MAX_SECONDS, decision_left - TURN_SAFETY_SECONDS, bank_left / 7) - (time.monotonic() - started)
        result, ans = None, None
        if budget >= 3.0:
            menus = [_slot_menu(rows) for rows in annotated]
            while len(menus) < 2:
                menus.append({"null:null": "pass"})
            baseline = {f"slot_{i}": _choice_id(plan[f"slot_{i}"]) for i in (0, 1)}
            for i in (0, 1):
                menus[i].setdefault(baseline[f"slot_{i}"], "code baseline choice")
            payload = {
                "decision": "doubles_turn",
                "board": analysis.board(),
                "time": {"decision_seconds_left": decision_left, "bank_seconds_left": bank_left},
                "slot_0_choices": menus[0],
                "slot_1_choices": menus[1],
                "code_baseline": baseline,
                "answer_format": "Pick one choice id for slot_0 and one for slot_1, copied exactly from the lists.",
            }

            def build(ans):
                picks = {}
                for i in (0, 1):
                    cid = str(ans.get(f"slot_{i}"))
                    if cid not in menus[i]:
                        raise ValueError(f"slot_{i} must be one of {list(menus[i])}")
                    opt, tgt = cid.split(":")
                    picks[f"slot_{i}"] = {"option": None if opt == "null" else int(opt),
                                          "target": None if tgt == "null" else int(tgt)}
                    rows = annotated[i] if i < len(annotated) else []
                    row = next((r for r in rows if r["option"] == picks[f"slot_{i}"]["option"]), None)
                    if row and row.get("value", 0) <= -0.45 and cid != baseline[f"slot_{i}"]:
                        raise ValueError(f"slot_{i} choice {cid} is a known-bad move here "
                                         f"({row.get('note') or 'it hits your own side or fails'}); pick another")
                if len(annotated) < 2:
                    picks["slot_1"] = plan["slot_1"]
                return choice.build(picks)

            result, ans = self.brain.ask(kind="doubles_turn", system=SYSTEM, payload=payload,
                                         schema=object_schema({"analysis": {"type": "string"},
                                                               "slot_0": {"type": "string", "enum": list(menus[0])},
                                                               "slot_1": {"type": "string", "enum": list(menus[1])},
                                                               "reasoning_summary": {"type": "string"}}),
                                         build=build, budget=budget)
        else:
            self.log(f"[pokemon] turn: low on time (budget {budget:.1f}s), playing code plan")
        self.bank_spent += time.monotonic() - started
        if result is None:
            analysis.remember(plan_action)
            self.log(f"[pokemon] turn {analysis.turn}: code plan {plan}")
            return WithReasoning(plan_action, "Playing the highest-value line.")
        analysis.remember(result)
        same = result == plan_action
        self.log(f"[pokemon] turn {analysis.turn}: {result['slot_0']} | {result['slot_1']} ({'= code plan' if same else 'model overrode code plan'}) | {ans.get('analysis', '')}")
        return WithReasoning(result, str(ans.get("reasoning_summary") or "")[:200])


ALLY_FRIENDLY_ATTACKS = {"pollenpuff", "beatup"}


def _choice_id(pick: dict) -> str:
    opt, tgt = pick.get("option"), pick.get("target")
    return f"{'null' if opt is None else opt}:{'null' if tgt is None else tgt}"


def _slot_menu(rows: list[dict]) -> dict[str, str]:
    """Every legal (option, target) pair for one slot, described in plain words with the code's estimates."""
    menu: dict[str, str] = {}
    for r in rows:
        kind = r.get("kind")
        if kind == "switch":
            menu[f"{r['option']}:null"] = (f"SWITCH to {r['switch_to']} ({r.get('hp_pct')}% HP; its best hit on foes "
                                          f"~{r.get('its_best_hit_on_foes_pct')}%, foes' best hit on it ~{r.get('foes_best_hit_on_it_pct')}%)")
            continue
        if kind != "move":
            menu[f"{r['option']}:null"] = kind.upper() if kind else "pass"
            continue
        name = r.get("move")
        base = f"{name} [{r.get('type')}, {r.get('category')}, BP {r.get('base_power')}" + (f", priority +{r['priority']}" if r.get("priority") else "") + "]"
        note = f" NOTE: {r['note']}." if r.get("note") else ""
        by_target = {t.get("target"): t for t in r.get("targets", [])}
        for tgt in r.get("valid_targets") or [None]:
            t = by_target.get(tgt, {})
            if "hits" in t:  # spread move
                parts = [f"{h['is']} ~{h.get('est_damage_pct_of_current_hp', 0)}% of current HP" + (" KO" if h.get("likely_ko") else "")
                         for h in t["hits"]]
                desc = f"{base} hits " + "; ".join(parts) if parts else base
            elif "est_damage_pct_of_current_hp" in t:
                if str(t.get("is", "")).startswith("YOUR") and to_id(name) not in ALLY_FRIENDLY_ATTACKS:
                    continue  # attacking your own ally is almost never right; keep the menu clean
                desc = (f"{base} → {t['is']}: ~{t['est_damage_pct_of_current_hp']}% of its current HP"
                        + (", likely KO" if t.get("likely_ko") else "") + (f", effectiveness x{t['effectiveness']}" if t.get("effectiveness", 1) != 1 else "")
                        + (", may be immune (ability)" if t.get("maybe_immune_by_ability") else ""))
            elif t.get("is"):
                desc = f"{base} → {t['is']}"
            else:
                desc = base
            menu[f"{r['option']}:{'null' if tgt is None else tgt}"] = desc + note
    return menu
