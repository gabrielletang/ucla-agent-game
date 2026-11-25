"""Our tournament agent: one object per match, routing each game to its player.

- Pokémon (pokemon_vgc_doubles_draft): agent/pokemon/player.py
- Werewolf:                            agent/werewolf/player.py
- Anything else: the starter's general LLM agent (examples/llm_agent.py)

In both games the code computes facts and a recommended move, and the LLM makes
the decision (course requirement: the LLM must meaningfully influence play).
If the model fails or runs out of time, the code recommendation is played, so
the agent never stalls.

Needs OPENAI_API_KEY in .env (optional OPENAI_MODEL, default gpt-4o-mini).
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState
from examples import smoke_agent

from .llm import Brain
from .pokemon.player import PokemonPlayer
from .werewolf.player import WerewolfPlayer


class TournamentAgent:
    def __init__(self, provider=None, log=print):
        self.brain = Brain(provider, log=log)
        self.log = log
        self._player = None
        self._fallback = None

    def _player_for(self, context: DecisionContext):
        if self._player is None:
            game = (context.game_type or "").lower()
            if game.startswith("pokemon"):
                self._player = PokemonPlayer(self.brain, log=self.log)
            elif "werewolf" in game:
                self._player = WerewolfPlayer(self.brain, log=self.log)
            else:
                from examples.llm_agent import LLMAgent
                self._player = LLMAgent(self.brain.provider, log=self.log)
        return self._player

    def choose_action(self, state: GameState, context: DecisionContext):
        try:
            return self._player_for(context).choose_action(state, context)
        except Exception as exc:  # last line of defence: a crash here would forfeit the game
            self.log(f"[agent] ERROR in our decision code ({type(exc).__name__}: {exc}); playing a safe legal move")
            return _safe_move(state)

    def choose_message(self, state: GameState, context: DecisionContext):
        player = self._player_for(context)
        if hasattr(player, "choose_message"):
            try:
                return player.choose_message(state, context)
            except Exception as exc:
                self.log(f"[agent] ERROR in our message code ({type(exc).__name__}: {exc}); ending discussion")
        return TERMINATE_MESSAGING

    def on_action_result(self, result, context):
        player = self._player
        if player is not None and hasattr(player, "on_action_result"):
            player.on_action_result(result, context)


def _safe_move(state: GameState):
    """The starter's always-valid move (handles Pokémon templates); in Werewolf, avoid abstaining."""
    non_abstain = [a for a in state.legal_actions if "abstain" not in (a.label or "").lower()]
    if non_abstain and not any(a.input.get("action") for a in state.legal_actions):
        return non_abstain[0]
    return smoke_agent.choose_action(state, None)


def create_agent():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # never overrides real env vars
    return TournamentAgent()
