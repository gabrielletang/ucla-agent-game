"""Offline tests for our tournament agent (agent/): fake model, fake game states.

What they check: every decision ends in a legal action, whether the model
answers well, answers badly, or never answers; and the code's own analysis
(damage estimates, wolf coordination, message budget) behaves sensibly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage, WithReasoning
from examples.llm.providers import ProviderError

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from pokemon_pool import POOL  # noqa: E402

from agent.agent import TournamentAgent  # noqa: E402

POKEMON = DecisionContext(session_id="s", tournament_id=None, game_type="pokemon_vgc_doubles_draft", seat_position=0, agent_id="me")
WEREWOLF = DecisionContext(session_id="w", tournament_id=None, game_type="werewolf", seat_position=0, agent_id="me")


class FakeProvider:
    """Answers with `answer(messages, name, schema)`; records every request."""

    model = "fake"

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def complete_structured(self, messages, schema_name, schema, timeout=None):
        payload = json.loads(messages[1]["content"])
        self.requests.append((schema_name, payload, schema))
        result = self.answer(payload, schema_name, schema)
        if isinstance(result, Exception):
            raise result
        return result


def failing(*_):
    return ProviderError("model down")


def make_agent(answer):
    provider = FakeProvider(answer)
    lines = []
    return TournamentAgent(provider, log=lines.append), provider, lines


def state(raw: dict) -> GameState:
    return GameState.from_mcp_state(raw)


def unwrap(decision):
    return decision.action if isinstance(decision, WithReasoning) else decision


# -- Pokémon ------------------------------------------------------------------------------


def draft_state(rosters=None, pool=POOL):
    obs = {"phase": "draft", "current_seat": "me", "available_cards": pool,
           "available_card_ids": [c["card_id"] for c in pool], "rosters": rosters or {"me": [], "opp": []},
           "picks_remaining": 12, "decision_deadline_at": None}
    actions = [{"action_id": f"draft_pick:{c['card_id']}", "label": c["species"],
                "input": {"action_id": f"draft_pick:{c['card_id']}", "action": {"type": "draft_pick", "card_id": c["card_id"]}}}
               for c in pool]
    return state({"session_id": "s", "status": "in_progress", "phase": "draft", "is_current_actor": True,
                  "is_terminal": False, "state_version": 1, "observation": obs,
                  "legal_actions": {"session_id": "s", "state_version": 1, "actions": actions}})


def test_draft_model_picks_from_the_code_shortlist():
    def answer(payload, name, schema):
        assert name == "draft_pick"
        second = payload["candidates_ranked_by_code"][1]["card_id"]
        return {"card_id": second, "reasoning_summary": "Fits the team."}

    agent, provider, _ = make_agent(answer)
    decision = agent.choose_action(draft_state(), POKEMON)
    shortlist = provider.requests[0][1]["candidates_ranked_by_code"]
    assert unwrap(decision).action_id == f"draft_pick:{shortlist[1]['card_id']}"
    assert 6 <= len(shortlist) <= 7


def test_draft_falls_back_to_code_pick_when_model_fails():
    agent, provider, lines = make_agent(failing)
    decision = agent.choose_action(draft_state(), POKEMON)
    assert unwrap(decision).action_id.startswith("draft_pick:")
    assert any("FALLBACK" in l for l in lines)


def test_draft_invalid_answer_is_retried_then_falls_back():
    agent, provider, _ = make_agent(lambda *_: {"card_id": "nope", "reasoning_summary": ""})
    decision = agent.choose_action(draft_state(), POKEMON)
    assert unwrap(decision).action_id.startswith("draft_pick:")
    assert len(provider.requests) == 2


def lineup_state(roster_species, opp_species):
    template = {"type": "select_lineup", "roster": roster_species, "bring_count": 4, "lead_count": 2,
                "instructions": "Choose exactly 4 and 2 leads."}
    obs = {"phase": "team_preview", "your_roster": [{"species": s} for s in roster_species],
           "opponent_roster": [{"species": s, "types": None} for s in opp_species]}
    return state({"session_id": "s", "status": "in_progress", "phase": "team_preview", "is_current_actor": True,
                  "is_terminal": False, "state_version": 2, "observation": obs,
                  "legal_actions": {"session_id": "s", "state_version": 2, "actions": [
                      {"action_id": "select_lineup", "label": "select_lineup",
                       "input": {"action_id": "select_lineup", "action": template}}]}})


MY6 = ["incineroar", "garchomp", "fluttermane", "amoonguss", "tornadus", "kingambit"]
OPP6 = ["rillaboom", "urshifurapidstrike", "gholdengo", "dragonite", "rotomwash", "torkoal"]


def test_lineup_uses_model_answer_and_code_baseline_is_valid():
    seen = {}

    def answer(payload, name, schema):
        seen["rec"] = payload["code_baseline"]
        return {"bring": MY6[:4], "leads": MY6[2:4], "reasoning_summary": "Good matchup."}

    agent, _, _ = make_agent(answer)
    agent.choose_action(draft_state(), POKEMON)  # draft once so the agent knows every card's set
    decision = unwrap(agent.choose_action(lineup_state(MY6, OPP6), POKEMON))
    assert decision == {"type": "select_lineup", "bring": MY6[:4], "leads": MY6[2:4]}
    rec = seen["rec"]
    assert len(set(rec["bring"])) == 4 and set(rec["leads"]) <= set(rec["bring"])


def test_lineup_fallback_is_legal():
    agent, _, _ = make_agent(failing)
    decision = unwrap(agent.choose_action(lineup_state(MY6, OPP6), POKEMON))
    assert len(set(decision["bring"])) == 4 and len(set(decision["leads"])) == 2
    assert set(decision["leads"]) <= set(decision["bring"]) <= set(MY6)


def mon(species, hp=1.0, **extra):
    return {"species": species, "current_hp_fraction": hp, "status": None, "boosts": {}, "fainted": False,
            "active": True, **extra}


def move_opt(move_id, targets, names):
    return {"type": "move", "move_id": move_id, "targets": targets,
            "target_options": [{"target": t, "side": names[t][0], "species": names[t][1]} for t in targets]}


def turn_state(turn=1, clock=None, my_hp=(1.0, 1.0), foe_hp=(1.0, 1.0)):
    names = {1: ("opponent", "amoonguss"), 2: ("opponent", "rotomwash"), -1: ("self", "incineroar"), -2: ("ally", "garchomp")}
    names0 = names
    names1 = {**names, -1: ("ally", "incineroar"), -2: ("self", "garchomp")}
    slot0 = [move_opt("fakeout", [1, 2, -2], names0), move_opt("flareblitz", [1, 2, -2], names0),
             move_opt("knockoff", [1, 2, -2], names0), {"type": "move", "move_id": "partingshot", "targets": [1, 2]},
             {"type": "switch", "species": "fluttermane"}, {"type": "switch", "species": "kingambit"}]
    slot1 = [{"type": "move", "move_id": "earthquake", "targets": []}, move_opt("dragonclaw", [1, 2, -1], names1),
             {"type": "move", "move_id": "rockslide", "targets": []}, {"type": "move", "move_id": "protect", "targets": []},
             {"type": "switch", "species": "fluttermane"}, {"type": "switch", "species": "kingambit"}]
    template = {"type": "doubles_turn", "target_legend": {"1": "opp A"}, "instructions": "one action per slot",
                "slots": [{"slot": 0, "force_switch": False, "options": slot0},
                          {"slot": 1, "force_switch": False, "options": slot1}]}
    obs = {"turn": turn, "is_doubles": True, "weather": None, "fields": [], "side_conditions": [],
           "opponent_side_conditions": [],
           "active_pokemon": [mon("Incineroar", my_hp[0], types=["FIRE", "DARK"]), mon("Garchomp", my_hp[1], types=["DRAGON", "GROUND"])],
           "opponent_active_pokemon": [mon("Amoonguss", foe_hp[0], types=["GRASS", "POISON"]), mon("Rotom-Wash", foe_hp[1], types=["ELECTRIC", "WATER"])],
           "team": [mon("Incineroar"), mon("Garchomp"), {**mon("Flutter Mane"), "active": False}, {**mon("Kingambit"), "active": False}],
           "force_switch": [False, False], **({"clock": clock} if clock else {})}
    return state({"session_id": "s", "status": "in_progress", "phase": "moving", "is_current_actor": True,
                  "is_terminal": False, "state_version": 5, "observation": obs,
                  "legal_actions": {"session_id": "s", "state_version": 5, "actions": [
                      {"action_id": "doubles_turn", "label": "doubles_turn",
                       "input": {"action_id": "doubles_turn", "action": template}}]}})


def test_turn_code_plan_is_sensible_and_model_sees_damage_numbers():
    seen = {}

    def answer(payload, name, schema):
        seen["payload"] = payload
        return {**payload["code_baseline"], "reasoning_summary": "Follow the plan."} | {
            "slot_0": payload["code_baseline"]["slot_0"], "slot_1": payload["code_baseline"]["slot_1"]}

    agent, _, _ = make_agent(lambda p, n, s: {"slot_0": p["code_baseline"]["slot_0"],
                                              "slot_1": p["code_baseline"]["slot_1"],
                                              "reasoning_summary": "Plan."} if not seen.update(payload=p) else None)
    agent.choose_action(draft_state(), POKEMON)  # learn the sets
    decision = unwrap(agent.choose_action(turn_state(), POKEMON))
    payload = seen["payload"]
    eq = next(d for d in payload["slot_1_choices"].values() if d.startswith("Earthquake"))
    assert "YOUR ALLY" in eq, "Earthquake must warn it hits the ally"
    assert "Rotom-Wash ~0%" in eq  # Levitate
    assert payload["code_baseline"]["slot_0"] in payload["slot_0_choices"]
    assert decision["type"] == "doubles_turn"
    # Turn 1: Fake Out or a strong Fire move from Incineroar; never target own ally.
    assert decision["slot_0"]["type"] == "move" and decision["slot_0"].get("target") in (1, 2)
    assert decision["slot_1"]["type"] in ("move", "switch")
    if decision["slot_1"]["type"] == "move":
        assert decision["slot_1"]["move_id"] != "earthquake"


def test_fake_out_is_marked_as_failing_after_first_turn():
    seen = []
    agent, _, _ = make_agent(lambda p, n, s: seen.append(p) or {"slot_0": p["code_baseline"]["slot_0"],
                                                                "slot_1": p["code_baseline"]["slot_1"],
                                                                "reasoning_summary": "x"})
    agent.choose_action(turn_state(turn=1), POKEMON)
    agent.choose_action(turn_state(turn=2), POKEMON)
    fake_out_t2 = [d for d in seen[1]["slot_0_choices"].values() if d.startswith("Fake Out")]
    assert fake_out_t2 and all("FAIL" in d for d in fake_out_t2)


def test_turn_falls_back_to_code_plan_and_skips_model_when_bank_is_low():
    agent, provider, lines = make_agent(failing)
    decision = unwrap(agent.choose_action(turn_state(), POKEMON))
    assert decision["type"] == "doubles_turn"
    agent2, provider2, lines2 = make_agent(lambda *_: pytest.fail("model must not be called"))
    decision2 = unwrap(agent2.choose_action(turn_state(clock={"decision_seconds_left": 12, "bank_seconds_left": 15}), POKEMON))
    assert decision2["type"] == "doubles_turn" and not provider2.requests


def test_model_answer_targeting_illegal_target_is_rejected_then_code_plan_played():
    agent, provider, _ = make_agent(lambda *_: {"slot_0": "1:7", "slot_1": "0:null", "reasoning_summary": "x"})
    decision = unwrap(agent.choose_action(turn_state(), POKEMON))
    assert decision["type"] == "doubles_turn" and len(provider.requests) == 2


# -- Werewolf ------------------------------------------------------------------------------

PLAYERS = [{"position": i, "name": f"Player{i}", "agent_id": f"a{i}"} for i in range(7)]


def ww_state(observation, *, sub_phase="day_vote", phase="moving", legal=(), day=2, alive=range(7), dead=(),
             votes=None, messages=(), me=0):
    actions = [{"action_id": str(a), "label": label, "input": {"session_id": "w", "action_id": str(a), "state_version": 4}}
               for a, label in legal]
    raw = {"session_id": "w", "status": "in_progress", "phase": phase, "is_current_actor": bool(actions),
           "is_terminal": False, "state_version": 4, "observation": observation, "your_position": me,
           "players": PLAYERS,
           "game_state": {"day": day, "phase": sub_phase, "alive": [f"Player{i}" for i in alive], "dead": list(dead),
                          "vote_history": votes or []},
           "new_messages": [{"index": i, "sender": s, "recipients": r, "content": c, "type": "chat",
                             "sent_at": "", "move_index": 0} for i, (s, r, c) in enumerate(messages)]}
    if actions:
        raw["legal_actions"] = {"session_id": "w", "state_version": 4, "actions": actions}
    return state(raw)


def vote_options(alive, me=0):
    return [(i, f"Vote to eliminate Player{i}") for i in alive if i != me] + [(7, "Abstain")]


def test_villager_vote_uses_model_and_code_suspicion_from_record():
    dead = [{"day": 1, "player": "Player3", "role": "villager", "cause": "vote"}]
    votes = [{"day": 1, "votes": {"Player1": "Player3", "Player2": "Player3", "Player4": "Player5",
                                  "Player5": "Player3", "Player6": "Player4", "Player0": "Player4"}}]
    alive = [0, 1, 2, 4, 5, 6]
    seen = {}

    def answer(payload, name, schema):
        seen["p"] = payload
        return {"action_id": "4", "reasoning_summary": "Suspicious votes."}

    agent, _, _ = make_agent(answer)
    s = ww_state("=== Werewolf ===\nYou are Player0. Your role: VILLAGER\nDay 2, alive 6/7", legal=vote_options(alive),
                 alive=alive, dead=dead, votes=votes)
    decision = unwrap(agent.choose_action(s, WEREWOLF))
    assert decision.action_id == "4"
    facts = seen["p"]["facts"]
    assert facts["my_role"] == "villager"
    assert facts["votes_for_later_revealed_villagers"]["P1"] == 1
    assert facts["parity"]["wolves_left"] == 2
    # Fallback (code) suggestion should be one of the players who voted out a villager, never abstain.
    assert seen["p"]["code_baseline_from_vote_record"] in {"1", "2", "5"}


def test_seer_reads_its_checks_and_votes_known_wolf_on_fallback():
    obs = "=== Werewolf ===\nYour role: SEER\nInvestigation: Player4 is a WOLF\nDay 2, alive 6/7"
    agent, _, _ = make_agent(failing)
    alive = [0, 1, 2, 4, 5, 6]
    decision = unwrap(agent.choose_action(ww_state(obs, legal=vote_options(alive), alive=alive), WEREWOLF))
    assert decision.action_id == "4"


def test_wolf_kill_follows_lower_seat_allys_private_proposal():
    obs = "=== Werewolf ===\nYour role: WEREWOLF\nYour fellow wolves: Player1\nNight 2, alive 7/7"
    agent, _, _ = make_agent(failing)
    me = 3
    # Day messages: ally Player1 privately proposes killing Player5.
    msg_state = ww_state(obs, phase="messaging", me=me, messages=[(1, [3], "Kill P5. Push P6 in the vote.")])
    agent.choose_message(msg_state, WEREWOLF)
    kill = ww_state(obs, sub_phase="night_wolf", me=me,
                    legal=[(i, f"Kill Player{i}") for i in (0, 2, 4, 5, 6)])
    decision = unwrap(agent.choose_action(kill, WEREWOLF))
    assert decision.action_id == "5"


def test_wolf_never_votes_its_ally_on_fallback():
    obs = "Your role: WEREWOLF\nYour fellow wolves: Player2\nDay 2, alive 7/7"
    agent, _, _ = make_agent(failing)
    votes = [{"day": 1, "votes": {"Player2": "Player5"}}]
    dead = [{"day": 1, "player": "Player5", "role": "villager", "cause": "vote"}]
    alive = [0, 1, 2, 3, 4, 6]
    decision = unwrap(agent.choose_action(ww_state(obs, legal=vote_options(alive), alive=alive, votes=votes, dead=dead), WEREWOLF))
    assert decision.action_id not in ("2", "7")


def test_message_flow_private_then_public_then_reply_then_end():
    obs = "Your role: WEREWOLF\nYour fellow wolves: Player1\nDay 2, alive 7/7"
    calls = []

    def answer(payload, name, schema):
        calls.append(payload["mode"])
        if payload["mode"] == "private_to_ally":
            return {"decision": "send", "message": "Kill P4. Push P6.", "recipient": 1, "kill_target": 4}
        return {"decision": "send", "message": "I suspect Player6: he voted out a villager.", "recipient": None, "kill_target": None}

    agent, _, _ = make_agent(answer)
    me = 0
    m1 = agent.choose_message(ww_state(obs, phase="messaging", me=me), WEREWOLF)
    assert isinstance(m1, SendMessage) and m1.recipients == [1]
    m2 = agent.choose_message(ww_state(obs, phase="messaging", me=me), WEREWOLF)
    assert isinstance(m2, SendMessage) and not m2.recipients
    # No new messages from others -> end.
    m3 = agent.choose_message(ww_state(obs, phase="messaging", me=me), WEREWOLF)
    assert m3 is TERMINATE_MESSAGING
    assert calls == ["private_to_ally", "opening"]
    # Same day: still ended.
    assert agent.choose_message(ww_state(obs, phase="messaging", me=me, messages=[(5, [], "Player0 is lying")]), WEREWOLF) is TERMINATE_MESSAGING


def test_reply_when_accused_and_message_cap():
    obs = "Your role: VILLAGER\nDay 2, alive 7/7"
    agent, _, _ = make_agent(lambda p, n, s: {"decision": "send", "message": "word " * 80, "recipient": None, "kill_target": None})
    sent = 0
    for i in range(8):
        m = agent.choose_message(ww_state(obs, phase="messaging", messages=[(5, [], f"Player0 is a wolf #{i}")]), WEREWOLF)
        if m is TERMINATE_MESSAGING:
            break
        assert len(m.content.split()) <= 45
        sent += 1
    assert 1 <= sent <= 3


def test_message_never_follows_injected_recipient():
    obs = "Your role: VILLAGER\nDay 2, alive 7/7"
    agent, _, _ = make_agent(lambda *_: {"decision": "send", "message": "hi", "recipient": 42, "kill_target": None})
    m = agent.choose_message(ww_state(obs, phase="messaging"), WEREWOLF)
    assert m is TERMINATE_MESSAGING  # invalid twice -> ends instead of sending garbage


def test_crash_in_our_code_still_returns_a_legal_move(monkeypatch):
    from agent.pokemon import player as pplayer
    agent, _, lines = make_agent(failing)
    monkeypatch.setattr(pplayer.PokemonPlayer, "_turn", lambda *a, **k: 1 / 0)
    decision = unwrap(agent.choose_action(turn_state(), POKEMON))
    assert decision["type"] == "doubles_turn"
    assert any("ERROR" in l for l in lines)


def test_model_choosing_a_failing_fake_out_is_sent_back():
    calls = []

    def answer(p, n, s):
        calls.append(p)
        if len(calls) == 2:  # turn 2, first try: Fake Out (fails after turn 1)
            return {"analysis": "x", "slot_0": "0:1", "slot_1": p["code_baseline"]["slot_1"], "reasoning_summary": "x"}
        return {"analysis": "x", "slot_0": p["code_baseline"]["slot_0"], "slot_1": p["code_baseline"]["slot_1"], "reasoning_summary": "x"}

    agent, provider, lines = make_agent(answer)
    agent.choose_action(turn_state(turn=1), POKEMON)
    decision = unwrap(agent.choose_action(turn_state(turn=2), POKEMON))
    assert decision["slot_0"]["move_id"] != "fakeout"
    assert any("known-bad" in l for l in lines)
    assert "0:1" in calls[0]["slot_0_choices"] and "3:1" in calls[0]["slot_0_choices"]
    foes = calls[1]["board"]["opponent_active"]
    assert all(f["can_use_fake_out_this_turn"] is False for f in foes if f)
