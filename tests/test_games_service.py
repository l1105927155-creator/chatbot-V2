import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

import pytest  # noqa: E402

from v2_games import engine2048  # noqa: E402
from v2_games.service import (  # noqa: E402
    GameCapability, GameError, GameService, InvalidReplay, ScoreMismatch,
    mask_qq, normalize_qq,
)
from v2_games.store import GameStore  # noqa: E402


def service(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    return store, GameService(store, {
        "public_base_url": "https://example.ts.net:8443",
        "leaderboard_limit": 20,
        "run_ttl_hours": 24,
    })


def play(seed, steps=20):
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


def test_qq_helpers():
    assert normalize_qq("10001") == "10001"
    assert mask_qq("10001") == "1000****"
    with pytest.raises(GameError):
        normalize_qq("abc")
    with pytest.raises(GameError):
        normalize_qq("1000")


def test_group_link_uses_configured_base(tmp_path):
    store, game = service(tmp_path)
    link = game.group_link("123")
    assert link["token"]
    assert link["link"] == f"https://example.ts.net:8443/g/{link['token']}"
    assert game.resolve_group_token(link["token"]) == "123"
    with pytest.raises(GameError):
        game.resolve_group_token("bad-token")
    store.close()


def test_valid_submission_and_leaderboard(tmp_path):
    store, game = service(tmp_path)
    run = game.create_web_run(group_id="123", qq="10001")
    seed = store.get_run(run["run_id"])["seed"]
    state, moves = play(seed, 10)
    result = game.submit_web_run(
        run_id=run["run_id"], group_id="123", qq="10001", moves=moves,
        client_score=state.score, max_tile=engine2048.max_tile(state),
        duration_ms=1200,
    )
    assert result["score"] == state.score
    assert result["rank"] == 1
    board = game.leaderboard(group_id="123", kind="human", limit=10)
    assert board["rows"][0]["display_name"] == "1000****"
    assert board["rows"][0]["score"] == state.score
    me = game.me(group_id="123", qq="10001")
    assert me["rank"] == 1 and me["best_score"] == state.score
    store.close()


def test_score_mismatch_is_rejected_and_run_closed(tmp_path):
    store, game = service(tmp_path)
    run = game.create_web_run(group_id="123", qq="10001")
    seed = store.get_run(run["run_id"])["seed"]
    state, moves = play(seed, 5)
    with pytest.raises(ScoreMismatch):
        game.submit_web_run(
            run_id=run["run_id"], group_id="123", qq="10001", moves=moves,
            client_score=state.score + 1, max_tile=engine2048.max_tile(state),
            duration_ms=1000,
        )
    assert store.get_run(run["run_id"])["status"] == "invalid"
    with pytest.raises(GameError):
        game.submit_web_run(
            run_id=run["run_id"], group_id="123", qq="10001", moves=moves,
            client_score=state.score, max_tile=engine2048.max_tile(state),
            duration_ms=1000,
        )
    store.close()


def test_invalid_replay_and_foreign_run_are_rejected(tmp_path):
    store, game = service(tmp_path)
    first = game.create_web_run(group_id="123", qq="10001")
    with pytest.raises(InvalidReplay):
        game.submit_web_run(
            run_id=first["run_id"], group_id="123", qq="10001", moves=[4],
            client_score=0, max_tile=0, duration_ms=1,
        )
    assert store.get_run(first["run_id"])["status"] == "invalid"
    second = game.create_web_run(group_id="123", qq="10002")
    with pytest.raises(GameError):
        game.submit_web_run(
            run_id=second["run_id"], group_id="123", qq="10001", moves=[0],
            client_score=0, max_tile=0, duration_ms=1,
        )
    store.close()


def test_bot_play_uses_server_engine_and_records_separate_kind(tmp_path):
    store, game = service(tmp_path)
    conversation = SimpleNamespace(key="group:123", bot_id="42")
    started = game.bot_start(conversation=conversation)
    run_id = started["run_id"]
    seed = store.get_run(run_id)["seed"]
    local = engine2048.new_game(seed)
    for _ in range(15):
        if local.over:
            break
        direction = next(d for d in range(4) if engine2048.can_move(local, d))
        result = game.bot_move(
            run_id=run_id, direction=direction, conversation=conversation)
        engine2048.move(local, direction)
        assert result["score"] == local.score
        assert result["moves"] == local.moves
    final = game.bot_finish(run_id=run_id, conversation=conversation)
    assert final["finished"] is True
    bot_board = game.leaderboard(group_id="123", kind="bot", limit=10)
    assert bot_board["rows"][0]["display_name"] == "Bot"
    assert bot_board["rows"][0]["score"] == final["score"]
    store.close()


def test_bot_game_auto_finishes_and_records_score(tmp_path):
    store, game = service(tmp_path)
    conversation = SimpleNamespace(key="group:123", bot_id="42")
    started = game.bot_start(conversation=conversation)
    run_id = started["run_id"]
    seed = store.get_run(run_id)["seed"]
    local = engine2048.new_game(seed)
    result = started
    for _ in range(500):
        if local.over:
            break
        direction = next(d for d in range(4) if engine2048.can_move(local, d))
        result = game.bot_move(
            run_id=run_id, direction=direction, conversation=conversation)
        engine2048.move(local, direction)
        assert result["score"] == local.score
        if result["finished"]:
            break
    assert local.over is True
    assert result["finished"] is True
    assert store.get_run(run_id)["status"] == "finished"
    row = game.leaderboard(group_id="123", kind="bot", limit=10)["rows"][0]
    assert row["score"] == local.score
    store.close()


def test_capability_queries_current_group_and_actor(tmp_path):
    store, game = service(tmp_path)
    conversation = SimpleNamespace(key="group:123", bot_id="42")
    capability = GameCapability(game, conversation, actor_id="10001")
    assert capability.list_games()[0]["id"] == "2048"
    board = capability.leaderboard(kind="human", limit=10)
    assert board["scope"] == "group"
    assert board["rows"] == []
    assert capability.my_scores()["best_score"] == 0
    store.close()
