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

SYSTEM = """You are a strong Pokémon VGC (Gen 9 doubles, level 50, no Terastallization) player controlling one side of a
draft battle. Each request is one decision. The server's options are authoritative: pick only from them.
Our code has pre-computed numbers for you (stats, type matchups, estimated damage as % of the target's CURRENT HP,
likely KOs, speed estimates) and a recommended choice. Treat the numbers as good estimates, not certainties.
Think like a VGC expert: focus fire to remove threats, respect speed order and priority, use Protect when a slot is
the obvious target, use Fake Out only on a Pokémon's first turn out, never hit your own ally unless it is immune or
the trade is clearly worth it, avoid wasting moves into immunities, keep win conditions alive.
Follow the recommendation unless you see a concrete reason to deviate.
reasoning_summary is PUBLIC (the opponent may read it): one short sentence, no secret plans."""

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
            "code_recommendation": shortlist[0]["card_id"],
            "guidance": "Build a TEAM: cover shared weaknesses, mix physical and special, have speed control and "
                        "Fake Out/Intimidate support. Also consider denying a card the opponent clearly needs.",
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
        value, ans = self.brain.ask(kind="draft_pick", system=SYSTEM, payload=payload,
                                    schema=object_schema({"card_id": {"type": "string", "enum": ids},
                                                          "reasoning_summary": {"type": "string"}}),
                                    build=build, budget=budget)
        if value is None:
            self.log(f"[pokemon] draft: FALLBACK to code pick {shortlist[0]['species']}")
            return WithReasoning(fallback, "Best available fit for the team.")
        pick = next(p for p in shortlist if p["card_id"] == value.action_id.split(":", 1)[1])
        self.log(f"[pokemon] draft: picked {pick['species']} (code #1 was {shortlist[0]['species']})")
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
            "code_recommendation": rec,
        }
        schema_species = {"type": "array", "items": {"type": "string", "enum": roster}}
        value, ans = self.brain.ask(kind="select_lineup", system=SYSTEM, payload=payload,
                                    schema=object_schema({"bring": schema_species, "leads": schema_species,
                                                          "reasoning_summary": {"type": "string"}}),
                                    build=choice.build, budget=PREVIEW_MAX_SECONDS)
        if value is None:
            self.log(f"[pokemon] lineup: FALLBACK to code lineup {rec}")
            return WithReasoning(rec, "Lineup chosen for the matchup.")
        self.log(f"[pokemon] lineup: bring {value['bring']} lead {value['leads']} (code: {rec['bring']} / {rec['leads']})")
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
            payload = {
                "decision": "doubles_turn",
                "board": analysis.board(),
                "time": {"decision_seconds_left": decision_left, "bank_seconds_left": bank_left},
                "slots": [
                    {"slot": i, "force_switch": bool(s.get("force_switch")), "options": annotated[i]}
                    for i, s in enumerate(analysis.slots)
                ],
                "targets_legend": "1/2 = opponent positions A/B; -1 = your slot 0, -2 = your slot 1; null = no target needed",
                "code_recommendation": plan,
                "answer_format": "For slot_0 and slot_1 give the option index from that slot's options and, for a move "
                                 "with targets, one of its listed target integers (else null).",
            }
            slot_schema = object_schema({"option": {"type": "integer"}, "target": {"type": ["integer", "null"]}})
            result, ans = self.brain.ask(kind="doubles_turn", system=SYSTEM, payload=payload,
                                         schema=object_schema({"slot_0": slot_schema, "slot_1": slot_schema,
                                                               "reasoning_summary": {"type": "string"}}),
                                         build=choice.build, budget=budget)
        else:
            self.log(f"[pokemon] turn: low on time (budget {budget:.1f}s), playing code plan")
        self.bank_spent += time.monotonic() - started
        if result is None:
            analysis.remember(plan_action)
            self.log(f"[pokemon] turn {analysis.turn}: code plan {plan}")
            return WithReasoning(plan_action, "Playing the highest-value line.")
        analysis.remember(result)
        same = result == plan_action
        self.log(f"[pokemon] turn {analysis.turn}: {result['slot_0']} | {result['slot_1']} ({'= code plan' if same else 'model overrode code plan'})")
        return WithReasoning(result, str(ans.get("reasoning_summary") or "")[:200])
