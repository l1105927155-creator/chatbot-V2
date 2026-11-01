import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_games import engine2048  # noqa: E402
from v2_games.store import GameStore  # noqa: E402


def make_run(store, **overrides):
    params = dict(
        run_id="run_1", game_id="2048", group_id="123", player_key="group:123:qq:10001",
        qq="10001", kind="human", seed=7, rules_version=1, source="web", now=1000,
    )
    params.update(overrides)
    return store.create_run(**params)


def play(seed, steps=5):
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


def test_group_tokens_are_stable_and_revocable(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    first = store.get_or_create_group_token("123")
    second = store.get_or_create_group_token("123")
    assert first == second
    assert store.group_id_for_token(first) == "123"
    assert store.group_id_for_token("missing") is None
    assert store.list_groups()[0]["group_id"] == "123"
    store.close()


def test_new_run_abandons_previous_active_run(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    make_run(store, run_id="run_1", now=1000)
    make_run(store, run_id="run_2", now=1001)
    first = store.get_run("run_1")
    second = store.get_run("run_2")
    assert first["status"] == "abandoned"
    assert second["status"] == "active"
    assert store.active_run("group:123:qq:10001")["run_id"] == "run_2"
    store.close()


def test_finish_is_idempotent_and_leaderboard_uses_server_score(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    make_run(store, now=1000)
    finished = store.finish_run(
        run_id="run_1", player_key="group:123:qq:10001", score=2048, max_tile=1024,
        moves=10, client_score=2048, moves_list=[0] * 10, duration_ms=1000,
        now=1001,
    )
    again = store.finish_run(
        run_id="run_1", player_key="group:123:qq:10001", score=4096, max_tile=2048,
        moves=11, client_score=4096, moves_list=[0] * 11, duration_ms=1000,
        now=1002,
    )
    assert finished["score"] == 2048
    assert again["score"] == 2048
    make_run(store, run_id="run_2", player_key="group:123:qq:20002",
             qq="20002", seed=8, now=1003)
    store.finish_run(
        run_id="run_2", player_key="group:123:qq:20002", score=1024, max_tile=512,
        moves=8, client_score=1024, moves_list=[0] * 8, duration_ms=1000,
        now=1004,
    )
    rows = store.leaderboard(game_id="2048", group_id="123", limit=10)
    assert [row["score"] for row in rows] == [2048, 1024]
    assert store.player_best(game_id="2048", player_key="group:123:qq:10001")["score"] == 2048
    store.close()


def test_leaderboard_filters_period_and_kind(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    make_run(store, run_id="old", seed=7, now=1000)
    store.finish_run(
        run_id="old", player_key="group:123:qq:10001", score=4096, max_tile=2048,
        moves=10, client_score=4096, moves_list=[0] * 10, duration_ms=1000, now=1001,
    )
    make_run(store, run_id="bot", player_key="group:123:bot:42", qq=None, kind="bot",
             seed=8, source="bot", now=2000)
    store.finish_run(
        run_id="bot", player_key="group:123:bot:42", score=8192, max_tile=4096,
        moves=10, client_score=8192, moves_list=[0] * 10, duration_ms=1000, now=2001,
    )
    assert [row["kind"] for row in store.leaderboard(game_id="2048", group_id="123")] == ["bot", "human"]
    assert len(store.leaderboard(game_id="2048", group_id="123", since=1900)) == 1
    assert store.leaderboard(game_id="2048", group_id="123", since=1900)[0]["kind"] == "bot"
    assert len(store.leaderboard(game_id="2048", group_id="123", kind="human")) == 1
    store.close()


def test_stale_active_runs_are_abandoned(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    make_run(store, now=1000)
    assert store.abandon_stale_active(before=2000, now=3000) == 1
    assert store.get_run("run_1")["status"] == "abandoned"
    store.close()


def test_update_active_moves_and_status_counts(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    make_run(store, now=1000)
    assert store.update_active_moves("run_1", "group:123:qq:10001", [0, 1], 2)
    row = store.get_run("run_1")
    assert row["moves"] == 2 and row["moves_json"] == "[0,1]"
    counts = store.status_counts()
    assert counts["runs_total"] == 1 and counts["runs_active"] == 1
    store.close()
