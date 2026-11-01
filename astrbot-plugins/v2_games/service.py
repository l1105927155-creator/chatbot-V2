"""Game service: identity, runs, replay verification, leaderboards and bot play."""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

from . import engine2048
from .catalog import get_game, list_games
from .store import GameStore

QQ_RE = re.compile(r"^[1-9][0-9]{4,11}$")


class GameError(RuntimeError):
    def __init__(self, public_message: str, *, code: str = "game_error") -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.code = code


class ScoreMismatch(GameError):
    def __init__(self, public_message: str) -> None:
        super().__init__(public_message, code="score_mismatch")


class InvalidReplay(GameError):
    def __init__(self, public_message: str) -> None:
        super().__init__(public_message, code="invalid_replay")


def normalize_qq(value: Any) -> str:
    if isinstance(value, bool):
        raise GameError("QQ 号格式无效。", code="invalid_qq")
    text = str(value or "").strip()
    if not QQ_RE.match(text):
        raise GameError("请输入 5-12 位 QQ 号。", code="invalid_qq")
    return text


def mask_qq(qq: str | None) -> str:
    if not qq:
        return "匿名"
    if len(qq) <= 4:
        return qq[0] + "*" * (len(qq) - 1)
    return qq[:4] + "****"


def group_id_from_key(key: str | None) -> str | None:
    if isinstance(key, str) and key.startswith("group:") and len(key) > 6:
        return key.split(":", 1)[1]
    return None


def human_player_key(group_id: str, qq: str) -> str:
    return f"group:{group_id}:qq:{qq}"


def bot_player_key(group_id: str | None, bot_id: str) -> str:
    if group_id:
        return f"group:{group_id}:bot:{bot_id}"
    return f"bot:{bot_id}"


class GameService:
    def __init__(self, store: GameStore, config: dict | Any, logger: Any = None) -> None:
        self.store = store
        self.config = config
        self.logger = logger
        try:
            stale = self.store.abandon_stale_active(int(time.time()) - 7 * 24 * 3600)
            if stale and logger is not None:
                logger.info("V2 games abandoned %s stale active runs", stale)
        except Exception:
            # Cleanup is best-effort; it must not prevent the plugin from loading.
            pass

    def _config(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def public_base_url(self) -> str:
        value = self._config("public_base_url", "")
        if not isinstance(value, str):
            return ""
        value = value.strip().rstrip("/")
        if not value.startswith(("https://", "http://")):
            return ""
        return value

    @property
    def leaderboard_limit(self) -> int:
        try:
            return max(1, min(int(self._config("leaderboard_limit", 20)), 50))
        except (TypeError, ValueError):
            return 20

    def group_link(self, group_id: str) -> dict[str, Any]:
        token = self.store.get_or_create_group_token(group_id)
        base = self.public_base_url
        return {
            "token": token,
            "link": f"{base}/g/{token}" if base else "",
        }

    def resolve_group_token(self, token: str) -> str:
        group_id = self.store.group_id_for_token(token)
        if not group_id:
            raise GameError("游戏链接无效或已失效。", code="group_not_found")
        return group_id

    def list_games(self) -> list[dict[str, Any]]:
        return [
            {"id": game.id, "title": game.title, "description": game.description}
            for game in list_games()
        ]

    def create_web_run(self, *, group_id: str, qq: str) -> dict[str, Any]:
        qq = normalize_qq(qq)
        game = get_game("2048")
        player_key = human_player_key(group_id, qq)
        run_id = "run_" + secrets.token_urlsafe(18)
        seed = secrets.randbits(32)
        now = int(time.time())
        self.store.create_run(
            run_id=run_id, game_id=game.id, group_id=group_id,
            player_key=player_key, qq=qq, kind="human", seed=seed,
            rules_version=game.rules_version, source="web", now=now,
        )
        return {
            "run_id": run_id,
            "game_id": game.id,
            "seed": seed,
            "rules_version": game.rules_version,
            "started_at": now,
        }

    def submit_web_run(self, *, run_id: str, group_id: str, qq: str,
                       moves: Any, client_score: Any, max_tile: Any,
                       duration_ms: Any) -> dict[str, Any]:
        qq = normalize_qq(qq)
        player_key = human_player_key(group_id, qq)
        run = self.store.get_run(run_id)
        if run is None:
            raise GameError("对局不存在。", code="run_not_found")
        if run["group_id"] != group_id or run["player_key"] != player_key:
            raise GameError("对局不属于当前玩家。", code="run_forbidden")
        if run["status"] == "finished":
            return self._run_result(run, group_id=group_id, qq=qq)
        if run["status"] != "active":
            raise GameError("对局已结束，不能再保存。", code="run_closed")
        game = get_game(str(run["game_id"]))
        if game.id != "2048":
            raise GameError("暂不支持该游戏。", code="game_unsupported")
        if type(moves) is not list or not moves:
            raise GameError("移动序列无效。", code="invalid_moves")
        if type(client_score) is not int or client_score < 0:
            raise GameError("分数格式无效。", code="invalid_score")
        if type(max_tile) is not int or max_tile < 0:
            raise GameError("最大块格式无效。", code="invalid_score")
        try:
            duration = max(0, min(int(duration_ms), 24 * 3600 * 1000))
        except (TypeError, ValueError):
            duration = 0
        try:
            state = engine2048.replay(int(run["seed"]), moves, game.max_moves)
        except ValueError as error:
            self.store.reject_run(run_id, player_key, reason="replay_invalid")
            raise InvalidReplay("回放校验失败，分数未保存。") from error
        if state.score != client_score or engine2048.max_tile(state) != max_tile:
            self.store.reject_run(run_id, player_key, reason="score_mismatch")
            raise ScoreMismatch("分数校验不一致，未保存。")
        row = self.store.finish_run(
            run_id=run_id, player_key=player_key, score=state.score,
            max_tile=engine2048.max_tile(state), moves=state.moves,
            client_score=client_score, moves_list=moves, duration_ms=duration,
        )
        return self._run_result(row, group_id=group_id, qq=qq)

    def abandon_web_run(self, *, run_id: str, group_id: str, qq: str) -> bool:
        return self.store.abandon_run(run_id, human_player_key(group_id, normalize_qq(qq)))

    def me(self, *, group_id: str, qq: str) -> dict[str, Any]:
        qq = normalize_qq(qq)
        player_key = human_player_key(group_id, qq)
        best = self.store.player_best(game_id="2048", player_key=player_key)
        recent = self.store.player_runs(game_id="2048", player_key=player_key, limit=5)
        rank = self._rank_for(player_key=player_key, group_id=group_id, qq=qq)
        return {
            "display_name": mask_qq(qq),
            "best_score": int(best["score"]) if best else 0,
            "best_max_tile": int(best["max_tile"]) if best else 0,
            "rank": rank,
            "recent": [self._public_run(row) for row in recent],
        }

    def leaderboard(self, *, group_id: str | None, period: str = "all",
                    kind: str = "human", limit: int | None = None,
                    viewer_key: str | None = None) -> dict[str, Any]:
        since = None
        if period == "week":
            since = int(time.time()) - 7 * 24 * 3600
        if period not in {"all", "week"}:
            raise GameError("排行周期无效。", code="invalid_period")
        kind_filter = None if kind == "all" else kind
        if kind_filter not in {None, "human", "bot"}:
            raise GameError("排行类型无效。", code="invalid_kind")
        rows = self.store.leaderboard(
            game_id="2048", group_id=group_id, since=since,
            kind=kind_filter,
            limit=max(1, min(int(limit or self.leaderboard_limit),
                             self.leaderboard_limit)),
        )
        result = []
        for index, row in enumerate(rows, start=1):
            item = self._public_run(row)
            item["rank"] = index
            item["is_me"] = bool(viewer_key and row["player_key"] == viewer_key)
            result.append(item)
        return {"period": period, "kind": kind, "scope": "group" if group_id else "global",
                "rows": result}

    def admin_leaderboard(self, *, group_id: str | None, period: str = "all",
                          kind: str = "all", limit: int = 20) -> dict[str, Any]:
        since = int(time.time()) - 7 * 24 * 3600 if period == "week" else None
        kind_filter = None if kind == "all" else kind
        rows = self.store.leaderboard(
            game_id="2048", group_id=group_id, since=since,
            kind=kind_filter, limit=max(1, min(int(limit), 100)),
        )
        return {"rows": [self._admin_run(row) for row in rows]}

    def admin_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._admin_run(row) for row in self.store.recent_runs(limit)]

    def status(self) -> dict[str, Any]:
        counts = self.store.status_counts()
        counts["public_base_url"] = self.public_base_url
        counts["groups"] = self.store.list_groups()
        return counts

    def _public_run(self, row: dict[str, Any]) -> dict[str, Any]:
        kind = row.get("kind") or "human"
        qq = row.get("qq")
        return {
            "display_name": "Bot" if kind == "bot" else mask_qq(qq),
            "kind": kind,
            "score": int(row.get("score") or 0),
            "max_tile": int(row.get("max_tile") or 0),
            "moves": int(row.get("moves") or 0),
            "ended_at": row.get("ended_at"),
            "source": row.get("source"),
            "status": row.get("status"),
        }

    @staticmethod
    def _admin_run(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": row.get("run_id"),
            "group_id": row.get("group_id"),
            "qq": row.get("qq"),
            "kind": row.get("kind"),
            "player_key": row.get("player_key"),
            "score": row.get("score"),
            "max_tile": row.get("max_tile"),
            "moves": row.get("moves"),
            "status": row.get("status"),
            "source": row.get("source"),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
        }

    def _rank_for(self, *, player_key: str, group_id: str | None,
                  qq: str | None) -> int | None:
        rows = self.store.leaderboard(game_id="2048", group_id=group_id, limit=100)
        for index, row in enumerate(rows, start=1):
            if group_id is not None:
                if row["player_key"] == player_key:
                    return index
            elif qq and row.get("qq") == qq:
                return index
        return None

    def _run_result(self, row: dict[str, Any], *, group_id: str, qq: str) -> dict[str, Any]:
        moves = self._run_moves(row)
        state = engine2048.replay(int(row["seed"]), moves)
        best = self.store.player_best(
            game_id="2048", player_key=human_player_key(group_id, qq))
        return {
            "run_id": row["run_id"],
            "score": int(row["score"] or state.score),
            "max_tile": int(row["max_tile"] or engine2048.max_tile(state)),
            "moves": int(row["moves"] or state.moves),
            "won": state.won,
            "over": state.over,
            "rank": self._rank_for(
                player_key=human_player_key(group_id, qq),
                group_id=group_id, qq=qq),
            "best_score": int(best["score"]) if best else 0,
            "state": engine2048.state_dict(state),
        }

    @staticmethod
    def _run_moves(row: dict[str, Any]) -> list[int]:
        raw = row.get("moves_json")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if type(data) is not list:
            return []
        return [int(value) for value in data if type(value) is int]

    # ---- Bot play -------------------------------------------------------

    def bot_start(self, *, conversation: Any) -> dict[str, Any]:
        group_id = group_id_from_key(getattr(conversation, "key", ""))
        bot_id = str(getattr(conversation, "bot_id", "") or "bot")
        player_key = bot_player_key(group_id, bot_id)
        active = self.store.active_run(player_key)
        if active is not None:
            return self.bot_state(run_id=active["run_id"], conversation=conversation)
        game = get_game("2048")
        run_id = "bot_" + secrets.token_urlsafe(18)
        seed = secrets.randbits(32)
        row = self.store.create_run(
            run_id=run_id, game_id=game.id, group_id=group_id,
            player_key=player_key, qq=None, kind="bot", seed=seed,
            rules_version=game.rules_version, source="bot",
        )
        return self._bot_payload(row, engine2048.new_game(int(row["seed"])), finished=False)

    def bot_state(self, *, run_id: str, conversation: Any) -> dict[str, Any]:
        row = self._bot_run(run_id, conversation)
        state = self._bot_replay(row)
        return self._bot_payload(row, state, finished=row["status"] == "finished")

    def bot_move(self, *, run_id: str, direction: str | int,
                 conversation: Any) -> dict[str, Any]:
        row = self._bot_run(run_id, conversation)
        if row["status"] != "active":
            state = self._bot_replay(row)
            return self._bot_payload(row, state, finished=True)
        state = self._bot_replay(row)
        moves = self._run_moves(row)
        try:
            direction_value = engine2048.resolve_direction(direction)
        except ValueError as error:
            raise GameError("方向无效。", code="invalid_direction") from error
        moved = engine2048.move(state, direction_value)
        if not moved:
            return self._bot_payload(row, state, finished=False, moved=False)
        moves.append(direction_value)
        now = int(time.time())
        duration = max(0, now - int(row["started_at"])) * 1000
        if state.over:
            finished = self.store.finish_run(
                run_id=run_id, player_key=row["player_key"], score=state.score,
                max_tile=engine2048.max_tile(state), moves=state.moves,
                client_score=state.score, moves_list=moves, duration_ms=duration,
            )
            return self._bot_payload(finished, state, finished=True, moved=True)
        self.store.update_active_moves(run_id, row["player_key"], moves, state.moves)
        return self._bot_payload(row, state, finished=False, moved=True)

    def bot_finish(self, *, run_id: str, conversation: Any) -> dict[str, Any]:
        row = self._bot_run(run_id, conversation)
        if row["status"] == "finished":
            return self.bot_state(run_id=run_id, conversation=conversation)
        if row["status"] != "active":
            raise GameError("对局已结束。", code="run_closed")
        state = self._bot_replay(row)
        moves = self._run_moves(row)
        now = int(time.time())
        finished = self.store.finish_run(
            run_id=run_id, player_key=row["player_key"], score=state.score,
            max_tile=engine2048.max_tile(state), moves=state.moves,
            client_score=state.score, moves_list=moves,
            duration_ms=max(0, now - int(row["started_at"])) * 1000,
        )
        return self._bot_payload(finished, state, finished=True)

    def _bot_run(self, run_id: str, conversation: Any) -> dict[str, Any]:
        row = self.store.get_run(run_id)
        if row is None or row["kind"] != "bot" or row["source"] != "bot":
            raise GameError("Bot 对局不存在。", code="run_not_found")
        group_id = group_id_from_key(getattr(conversation, "key", ""))
        bot_id = str(getattr(conversation, "bot_id", "") or "bot")
        if row["player_key"] != bot_player_key(group_id, bot_id):
            raise GameError("Bot 对局不属于当前会话。", code="run_forbidden")
        return row

    def _bot_replay(self, row: dict[str, Any]) -> engine2048.Game2048State:
        return engine2048.replay(int(row["seed"]), self._run_moves(row))

    def _bot_payload(self, row: dict[str, Any], state: engine2048.Game2048State,
                     *, finished: bool, moved: bool | None = None) -> dict[str, Any]:
        payload = {
            "run_id": row["run_id"],
            "score": state.score,
            "max_tile": engine2048.max_tile(state),
            "moves": state.moves,
            "won": state.won,
            "over": state.over,
            "finished": finished,
            "state": engine2048.state_dict(state),
            "text": engine2048.text_state(state),
        }
        if moved is not None:
            payload["moved"] = moved
        return payload


class GameCapability:
    """Conversation-bound adapter used by the DSH MCP tools."""

    def __init__(self, service: GameService, conversation: Any,
                 actor_id: str | None = None) -> None:
        self.service = service
        self.conversation = conversation
        self.actor_id = actor_id
        self.group_id = group_id_from_key(getattr(conversation, "key", ""))
        self.bot_id = str(getattr(conversation, "bot_id", "") or "bot")

    def list_games(self) -> list[dict[str, Any]]:
        return self.service.list_games()

    def leaderboard(self, *, period: str = "all", kind: str = "human",
                    limit: int = 10) -> dict[str, Any]:
        viewer = (human_player_key(self.group_id, self.actor_id)
                  if self.group_id and self.actor_id else None)
        return self.service.leaderboard(
            group_id=self.group_id, period=period, kind=kind, limit=limit,
            viewer_key=viewer)

    def global_leaderboard(self, *, period: str = "all", kind: str = "human",
                           limit: int = 10) -> dict[str, Any]:
        return self.service.leaderboard(
            group_id=None, period=period, kind=kind, limit=limit)

    def my_scores(self) -> dict[str, Any]:
        if not self.group_id or not self.actor_id:
            raise GameError("当前会话不是可查询的群聊。", code="not_group")
        return self.service.me(group_id=self.group_id, qq=self.actor_id)

    def bot_start(self) -> dict[str, Any]:
        return self.service.bot_start(conversation=self.conversation)

    def bot_state(self, run_id: str) -> dict[str, Any]:
        return self.service.bot_state(run_id=run_id, conversation=self.conversation)

    def bot_move(self, run_id: str, direction: str) -> dict[str, Any]:
        return self.service.bot_move(
            run_id=run_id, direction=direction, conversation=self.conversation)

    def bot_finish(self, run_id: str) -> dict[str, Any]:
        return self.service.bot_finish(
            run_id=run_id, conversation=self.conversation)


__all__ = [
    "GameService", "GameCapability", "GameError", "ScoreMismatch",
    "InvalidReplay", "normalize_qq", "mask_qq", "human_player_key",
    "bot_player_key", "group_id_from_key",
]
