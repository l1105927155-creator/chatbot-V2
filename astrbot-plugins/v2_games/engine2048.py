"""Authoritative 2048 rules and deterministic replay.

The browser uses the same rules and the same Mulberry32 sequence, but its score
is never trusted: the plugin replays the submitted direction list from the run
seed and computes the score itself. Bot runs use this engine directly.
"""

from __future__ import annotations

from typing import Callable

SIZE = 4
WIN_VALUE = 2048

DIRECTIONS: dict[str, int] = {"up": 0, "right": 1, "down": 2, "left": 3}
DIRECTION_NAMES = ("up", "right", "down", "left")


class Mulberry32:
    """Small, explicit 32-bit PRNG shared with the browser implementation."""

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("seed must be an unsigned 32-bit integer")
        self._state = seed

    def __call__(self) -> float:
        self._state = (self._state + 0x6D2B79F5) & 0xFFFFFFFF
        a = self._state
        t = ((a ^ (a >> 15)) * (a | 1)) & 0xFFFFFFFF
        product = ((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF
        t = ((t + product) & 0xFFFFFFFF) ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296


class Game2048State:
    __slots__ = ("grid", "score", "won", "over", "moves", "rng")

    def __init__(self, grid: list[list[int | None]], rng: Callable[[], float],
                 score: int = 0, won: bool = False, over: bool = False,
                 moves: int = 0) -> None:
        self.grid = grid
        self.score = score
        self.won = won
        self.over = over
        self.moves = moves
        self.rng = rng


def _empty_grid() -> list[list[int | None]]:
    return [[None for _ in range(SIZE)] for _ in range(SIZE)]


def _empty_cells(grid: list[list[int | None]]) -> list[tuple[int, int]]:
    return [(r, c) for r in range(SIZE) for c in range(SIZE) if grid[r][c] is None]


def _spawn(grid: list[list[int | None]], rng: Callable[[], float]) -> None:
    empty = _empty_cells(grid)
    if not empty:
        return
    r, c = empty[int(rng() * len(empty))]
    grid[r][c] = 2 if rng() < 0.9 else 4


def new_game(seed: int) -> Game2048State:
    rng = Mulberry32(seed)
    state = Game2048State(_empty_grid(), rng)
    _spawn(state.grid, rng)
    _spawn(state.grid, rng)
    return state


def _slide_line(line: list[int | None]) -> tuple[bool, int]:
    kept = [value for value in line if value is not None]
    merged: list[int] = []
    gained = 0
    index = 0
    while index < len(kept):
        if index + 1 < len(kept) and kept[index] == kept[index + 1]:
            value = kept[index] * 2
            merged.append(value)
            gained += value
            index += 2
        else:
            merged.append(kept[index])
            index += 1
    while len(merged) < SIZE:
        merged.append(0)
    moved = any((value or 0) != (merged[position] or 0)
                for position, value in enumerate(line))
    for position in range(SIZE):
        line[position] = merged[position] or None
    return moved, gained


def _extract(grid: list[list[int | None]], axis: str, index: int,
             reverse: bool) -> list[int | None]:
    line = [grid[index][position] if axis == "row" else grid[position][index]
            for position in range(SIZE)]
    return list(reversed(line)) if reverse else line


def _write(grid: list[list[int | None]], axis: str, index: int, reverse: bool,
           line: list[int | None]) -> None:
    data = list(reversed(line)) if reverse else line
    for position in range(SIZE):
        if axis == "row":
            grid[index][position] = data[position]
        else:
            grid[position][index] = data[position]


def _could_move(grid: list[list[int | None]]) -> bool:
    for r in range(SIZE):
        for c in range(SIZE):
            value = grid[r][c]
            if value is None:
                return True
            if r + 1 < SIZE and grid[r + 1][c] == value:
                return True
            if c + 1 < SIZE and grid[r][c + 1] == value:
                return True
    return False


def can_move(state: Game2048State, direction: int) -> bool:
    axis = "col" if direction in (0, 2) else "row"
    reverse = direction in (1, 2)
    for index in range(SIZE):
        line = _extract(state.grid, axis, index, reverse)
        moved, _ = _slide_line(line)
        if moved:
            return True
    return False


def move(state: Game2048State, direction: int) -> bool:
    """Apply one direction. Returns False when the move changes nothing."""
    if state.over:
        return False
    if direction not in (0, 1, 2, 3):
        raise ValueError("direction must be 0..3")
    axis = "col" if direction in (0, 2) else "row"
    reverse = direction in (1, 2)
    moved = False
    gained = 0
    for index in range(SIZE):
        line = _extract(state.grid, axis, index, reverse)
        line_moved, line_gained = _slide_line(line)
        if line_moved:
            moved = True
            gained += line_gained
            _write(state.grid, axis, index, reverse, line)
    if not moved:
        return False
    state.score += gained
    state.moves += 1
    if any(value is not None and value >= WIN_VALUE
           for row in state.grid for value in row):
        state.won = True
    _spawn(state.grid, state.rng)
    if not _could_move(state.grid):
        state.over = True
    return True


def max_tile(state: Game2048State) -> int:
    return max((value or 0 for row in state.grid for value in row), default=0)


def legal_directions(state: Game2048State) -> list[str]:
    if state.over:
        return []
    return [DIRECTION_NAMES[direction] for direction in range(4)
            if can_move(state, direction)]


def replay(seed: int, moves: list[int], max_moves: int = 5000) -> Game2048State:
    if type(moves) is not list:
        raise ValueError("moves must be a list")
    if len(moves) > max_moves:
        raise ValueError("移动序列过长")
    for direction in moves:
        if type(direction) is not int or direction not in (0, 1, 2, 3):
            raise ValueError("移动序列包含无效方向")
    state = new_game(seed)
    for direction in moves:
        if state.over:
            raise ValueError("对局结束后仍有移动")
        if not move(state, direction):
            raise ValueError("移动序列包含无效移动")
    return state


def state_dict(state: Game2048State) -> dict:
    return {
        "grid": [[value for value in row] for row in state.grid],
        "score": state.score,
        "won": state.won,
        "over": state.over,
        "moves": state.moves,
    }


def text_state(state: Game2048State) -> str:
    rows = "\n".join(" ".join("." if value is None else str(value) for value in row)
                     for row in state.grid)
    status = "over" if state.over else "won" if state.won else "playing"
    legal = ",".join(legal_directions(state)) or "none"
    return (f"game=2048 score={state.score} max={max_tile(state)} "
            f"moves={state.moves} status={status}\n{rows}\nlegal={legal}")


def resolve_direction(value: str | int) -> int:
    if type(value) is int and value in (0, 1, 2, 3):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in DIRECTIONS:
            return DIRECTIONS[normalized]
    raise ValueError("direction must be up/right/down/left or 0..3")


__all__ = [
    "SIZE", "WIN_VALUE", "DIRECTIONS", "DIRECTION_NAMES", "Mulberry32",
    "Game2048State", "new_game", "move", "can_move", "replay", "max_tile",
    "legal_directions", "state_dict", "text_state", "resolve_direction",
]
