"""Game registry for the V2 local mini-game hub.

Only deterministic, server-verifiable games are listed here. A game definition
does not contain UI or network code; the web bundle and engine are separate so
adding a game cannot expand the score/authorization contract implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GameDefinition:
    id: str
    title: str
    description: str
    rules_version: int
    max_moves: int
    engine: str


GAMES: dict[str, GameDefinition] = {
    "2048": GameDefinition(
        id="2048",
        title="2048",
        description="方向键滑动合并数字，合成 2048 后继续挑战更高分。",
        rules_version=1,
        max_moves=5000,
        engine="engine2048",
    ),
}


def get_game(game_id: str) -> GameDefinition:
    game = GAMES.get(game_id)
    if game is None:
        raise ValueError("未知游戏")
    return game


def list_games() -> list[GameDefinition]:
    return list(GAMES.values())


__all__ = ["GameDefinition", "GAMES", "get_game", "list_games"]
