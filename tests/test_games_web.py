import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from starlette.testclient import TestClient  # noqa: E402

from v2_games import engine2048  # noqa: E402
from v2_games.service import GameService  # noqa: E402
from v2_games.store import GameStore  # noqa: E402
from v2_games.webapp import GameWebApp  # noqa: E402

WEB_DIR = ROOT / "astrbot-plugins/v2_games/web"


def app_for(tmp_path):
    store = GameStore(tmp_path / "games.sqlite3")
    service = GameService(store, {
        "public_base_url": "https://example.tail3a495b.ts.net:8443",
        "leaderboard_limit": 20,
    })
    app = GameWebApp(service, secret=b"x" * 32, web_dir=WEB_DIR).app()
    return store, service, app


def play(seed, steps=10):
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


def test_web_identity_run_submit_and_leaderboard(tmp_path):
    store, service, app = app_for(tmp_path)
    token = service.group_link("123")["token"]
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/games").json()["games"][0]["id"] == "2048"
        assert client.get(f"/api/g/{token}/meta").json()["identity"] is None
        assert client.get(f"/g/{token}").status_code == 200
        assert client.get("/g/bad-token").status_code == 404
        assert client.get("/assets/style.css").status_code == 200

        response = client.post(f"/api/g/{token}/identity", json={"qq": "10001"})
        assert response.status_code == 200
        assert response.json()["me"]["display_name"] == "1000****"
        me = client.get(f"/api/g/{token}/me")
        assert me.status_code == 200 and me.json()["best_score"] == 0

        run = client.post(f"/api/g/{token}/runs", json={}).json()
        seed = store.get_run(run["run_id"])["seed"]
        state, moves = play(seed, 8)
        saved = client.post(f"/api/g/{token}/runs/{run['run_id']}/submit", json={
            "moves": moves,
            "client_score": state.score,
            "max_tile": engine2048.max_tile(state),
            "duration_ms": 1200,
        })
        assert saved.status_code == 200
        assert saved.json()["result"]["score"] == state.score
        board = client.get(f"/api/g/{token}/leaderboard?kind=human").json()
        assert board["rows"][0]["display_name"] == "1000****"
        assert board["rows"][0]["score"] == state.score
        assert "qq" not in board["rows"][0]
    store.close()


def test_web_rejects_bad_score_origin_and_unknown_run(tmp_path):
    store, service, app = app_for(tmp_path)
    token = service.group_link("123")["token"]
    with TestClient(app, base_url="https://testserver") as client:
        client.post(f"/api/g/{token}/identity", json={"qq": "10001"})
        run = client.post(f"/api/g/{token}/runs", json={}).json()
        seed = store.get_run(run["run_id"])["seed"]
        state, moves = play(seed, 5)
        mismatch = client.post(f"/api/g/{token}/runs/{run['run_id']}/submit", json={
            "moves": moves,
            "client_score": state.score + 1,
            "max_tile": engine2048.max_tile(state),
            "duration_ms": 1,
        })
        assert mismatch.status_code == 409
        assert store.get_run(run["run_id"])["status"] == "invalid"
        bad_origin = client.post(f"/api/g/{token}/runs", json={},
                                 headers={"Origin": "https://evil.example"})
        assert bad_origin.status_code == 403
        missing = client.post(f"/api/g/{token}/runs/not-a-run/submit", json={})
        assert missing.status_code == 404
    store.close()


def test_web_requires_identity_and_valid_group(tmp_path):
    store, service, app = app_for(tmp_path)
    token = service.group_link("123")["token"]
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(f"/api/g/{token}/me").status_code == 401
        assert client.post(f"/api/g/{token}/runs", json={}).status_code == 401
        assert client.post(f"/api/g/{token}/identity", json={"qq": "abc"}).status_code == 400
        assert client.get("/api/g/unknown/meta").status_code == 404
    store.close()
