"""Standalone HTTP check for v2_games without AstrBot.

Run from the repository root:
    LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu \
      .runtime/astrbot/.venv/bin/python scripts/check-games.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_games import engine2048  # noqa: E402
from v2_games.service import GameService  # noqa: E402
from v2_games.store import GameStore  # noqa: E402
from v2_games.webapp import GameWebApp, GameWebServer  # noqa: E402


def play(seed: int, steps: int = 10):
    state = engine2048.new_game(seed)
    moves = []
    for _ in range(steps):
        for direction in range(4):
            if engine2048.can_move(state, direction):
                moves.append(direction)
                engine2048.move(state, direction)
                break
        else:
            break
    return state, moves


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = GameStore(Path(tmp) / "games.sqlite3")
        service = GameService(store, {"public_base_url": "", "leaderboard_limit": 20})
        app = GameWebApp(service, secret=b"c" * 32,
                         web_dir=ROOT / "astrbot-plugins/v2_games/web").app()
        server = GameWebServer(app, port=0)
        await server.start()
        base = f"http://127.0.0.1:{server.port}"
        token = service.group_link("123")["token"]
        checks = {}
        try:
            async with httpx.AsyncClient(base_url=base) as client:
                assert (await client.get("/healthz")).status_code == 200
                checks["health"] = True
                assert (await client.get("/api/games")).json()["games"][0]["id"] == "2048"
                checks["catalog"] = True
                assert (await client.get(f"/api/g/{token}/me")).status_code == 401
                checks["identity_required"] = True
                identity = await client.post(f"/api/g/{token}/identity", json={"qq": "10001"})
                assert identity.status_code == 200
                assert identity.json()["me"]["display_name"] == "1000****"
                checks["masked_identity"] = True
                run = (await client.post(f"/api/g/{token}/runs", json={})).json()
                seed = store.get_run(run["run_id"])["seed"]
                state, moves = play(seed, 10)
                saved = await client.post(
                    f"/api/g/{token}/runs/{run['run_id']}/submit",
                    json={"moves": moves, "client_score": state.score,
                          "max_tile": engine2048.max_tile(state), "duration_ms": 1000})
                assert saved.status_code == 200
                checks["replay_submit"] = True
                board = (await client.get(
                    f"/api/g/{token}/leaderboard?kind=human")).json()["rows"]
                assert board and board[0]["display_name"] == "1000****"
                assert "qq" not in board[0]
                checks["masked_leaderboard"] = True
                bad_run = (await client.post(f"/api/g/{token}/runs", json={})).json()
                bad_seed = store.get_run(bad_run["run_id"])["seed"]
                bad_state, bad_moves = play(bad_seed, 5)
                bad = await client.post(
                    f"/api/g/{token}/runs/{bad_run['run_id']}/submit",
                    json={"moves": bad_moves, "client_score": 999999,
                          "max_tile": engine2048.max_tile(bad_state), "duration_ms": 1})
                assert bad.status_code == 409, bad.text
                checks["mismatch_rejected"] = True
                assert (await client.get("/api/g/unknown/meta")).status_code == 404
                checks["bad_group"] = True
        finally:
            await server.close()
            store.close()
        print(json.dumps(checks, ensure_ascii=False))
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
