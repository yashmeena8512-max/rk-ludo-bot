"""Small HTTP API that exposes the authoritative Python Ludo service."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlparse

from database import GameError, GameState, LudoDatabase
from ludo_engine import absolute_position, legal_token_indexes

logger = logging.getLogger(__name__)


class ApiError(ValueError):
    """An HTTP error with a status code safe to return to the Mini App."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _verify_init_data(init_data: str, bot_token: str) -> dict[str, object]:
    """Validate Telegram WebApp initData and return its user payload."""
    if os.environ.get("LUDO_ALLOW_DEMO_AUTH") == "1":
        if init_data == "demo":
            return {"id": 900001, "first_name": "Demo Player", "username": "demo"}
        if init_data.startswith("demo:"):
            demo_number = init_data.removeprefix("demo:")
            if demo_number.isdigit() and int(demo_number) in range(1, 10):
                number = int(demo_number)
                return {
                    "id": 900000 + number,
                    "first_name": f"Demo Player {number}",
                    "username": f"demo{number}",
                }
    if not init_data:
        raise ApiError("Open RK Ludo from Telegram to continue.", 401)

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ApiError("Telegram identity is missing.", 401)
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs.items())
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise ApiError("Telegram identity could not be verified.", 401)

    try:
        user = json.loads(pairs["user"])
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ApiError("Telegram user information is invalid.", 401) from error
    return user


def _player_payload(player, color: str | None = None) -> dict[str, object]:
    return {
        "telegramId": player.telegram_id,
        "firstName": player.first_name,
        "username": player.username,
        "color": color,
        "coins": player.coins,
        "gamesPlayed": player.games_played,
        "wins": player.wins,
        "losses": player.losses,
    }


def _state_payload(state: GameState) -> dict[str, object]:
    players: list[dict[str, object]] = []
    for player in state.players:
        players.append(
            {
                "telegramId": player.telegram_id,
                "firstName": player.first_name,
                "username": player.username,
                "slot": player.slot,
                "color": player.color,
                "tokens": [
                    {
                        "index": index,
                        "progress": progress,
                        "finished": progress == 57,
                        "absolutePosition": absolute_position(player.color, progress),
                    }
                    for index, progress in enumerate(state.tokens[player.telegram_id])
                ],
            }
        )
    return {
        "roomId": state.room_id,
        "status": state.status,
        "currentTurnId": state.current_turn_id,
        "diceResult": state.dice_result,
        "rolled": state.rolled,
        "winnerId": state.winner_id,
        "players": players,
        "createdAt": state.created_at,
        "updatedAt": state.updated_at,
    }


def _action_payload(
    state: GameState,
    *,
    dice: int | None = None,
    legal_tokens: tuple[int, ...] = (),
    captured: tuple[tuple[int, int], ...] = (),
    message: str | None = None,
) -> dict[str, object]:
    return {
        "game": _state_payload(state),
        "dice": dice,
        "legalTokenIndexes": list(legal_tokens),
        "capturedTokens": [f"{player_id}:{token_index}" for player_id, token_index in captured],
        "message": message,
    }


class LudoApiHandler(BaseHTTPRequestHandler):
    """Translate JSON HTTP requests into locked database operations."""

    database: LudoDatabase
    bot_token: str

    def log_message(self, format: str, *args) -> None:
        logger.info("Mini App API %s", format % args)

    def do_OPTIONS(self) -> None:
        self._send_json({}, 204)

    def do_GET(self) -> None:
        try:
            path = PurePosixPath(urlparse(self.path).path)
            parts = path.parts[1:] if path.parts and path.parts[0] == "/" else path.parts
            if str(path) in ("/", "/index.html"):
                try:
                    with open("index.html", "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except FileNotFoundError:
                    raise ApiError("Frontend UI (index.html) not found.", 404)
            if path == PurePosixPath("/health"):
                self._send_json({"status": "ok"})
                return
            if path == PurePosixPath("/ludo/leaderboard"):
                payload = [
                    _player_payload(player)
                    for player in self.database.get_leaderboard()
                ]
                self._send_json(payload)
                return
            if len(parts) == 3 and parts[:2] == ("ludo", "games"):
                room_id = parts[2]
                with self.database._lock:
                    connection = self.database._connection
                    if not connection.execute(
                        "SELECT 1 FROM games WHERE room_id = ?", (room_id,)
                    ).fetchone():
                        raise ApiError("Game not found.", 404)
                    snapshot = self.database._load_game(connection, room_id)
                self._send_json(_action_payload(snapshot))
                return
            raise ApiError("Endpoint not found.", 404)
        except ApiError as error:
            self._send_error(error)

    def do_POST(self) -> None:
        try:
            path = PurePosixPath(urlparse(self.path).path)
            parts = path.parts[1:] if path.parts and path.parts[0] == "/" else path.parts
            body = self._read_json()
            if path == PurePosixPath("/ludo/session"):
                user = self._authenticate(body)
                player = self._upsert_user(user)
                active_game = self.database.get_active_game_for_player(player.telegram_id)
                self._send_json(
                    {
                        "player": _player_payload(player),
                        "activeGame": _state_payload(active_game)
                        if active_game
                        else None,
                        "leaderboard": [
                            _player_payload(item)
                            for item in self.database.get_leaderboard()
                        ],
                    }
                )
                return

            user = self._authenticate(body)
            player = self._upsert_user(user)
            if path == PurePosixPath("/ludo/games"):
                state = self.database.create_game(
                    player.telegram_id, player.first_name, player.username
                )
                self._send_json(_action_payload(state), 201)
                return
            if path == PurePosixPath("/ludo/games/join"):
                room_id = str(body.get("roomId", "")).strip().upper()
                state = self.database.join_game(
                    room_id, player.telegram_id, player.first_name, player.username
                )
                self._send_json(_action_payload(state))
                return

            if len(parts) != 4 or parts[:2] != ("ludo", "games"):
                raise ApiError("Endpoint not found.", 404)
            room_id = parts[2]
            action = parts[3]
            if action == "roll":
                outcome = self.database.roll_dice(room_id, player.telegram_id)
                message = (
                    f"You rolled {outcome.dice}. No legal move was available."
                    if outcome.passed
                    else f"You rolled {outcome.dice}."
                )
                self._send_json(
                    _action_payload(
                        outcome.state,
                        dice=outcome.dice,
                        legal_tokens=outcome.legal_tokens,
                        message=message,
                    )
                )
                return
            if action == "move":
                token_index = body.get("tokenIndex")
                if not isinstance(token_index, int):
                    raise ApiError("Token index is required.")
                outcome = self.database.move_token(
                    room_id, player.telegram_id, token_index
                )
                self._send_json(
                    _action_payload(
                        outcome.state,
                        captured=outcome.captured_tokens,
                        message=(
                            "You captured a token."
                            if outcome.captured_tokens
                            else "Move completed."
                        ),
                    )
                )
                return
            if action == "leave":
                state = self.database.leave_game(room_id, player.telegram_id)
                self._send_json(_action_payload(state, message="You left the game."))
                return
            raise ApiError("Endpoint not found.", 404)
        except GameError as error:
            self._send_error(ApiError(str(error)))
        except ApiError as error:
            self._send_error(error)
        except Exception:
            logger.exception("Mini App API request failed")
            self._send_error(ApiError("The game service is temporarily unavailable.", 500))

    def _authenticate(self, body: dict[str, object]) -> dict[str, object]:
        init_data = body.get("initData")
        if not isinstance(init_data, str):
            raise ApiError("Telegram initData is required.", 401)
        return _verify_init_data(init_data, self.bot_token)

    def _upsert_user(self, user: dict[str, object]):
        telegram_id = user["id"]
        first_name = str(user.get("first_name") or "Player")
        username = user.get("username")
        return self.database.upsert_player(
            telegram_id,
            first_name,
            str(username) if username else None,
        )

    def _read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError("Request body must be valid JSON.") from error

    def _send_error(self, error: ApiError) -> None:
        self._send_json({"error": str(error)}, error.status)

    def _send_json(self, payload: object, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if status != 204:
            self.wfile.write(encoded)


def start_api_server(
    database: LudoDatabase,
    bot_token: str,
    port: int = 8000,
) -> ThreadingHTTPServer:
    """Start the Mini App API beside the Telegram polling loop."""
    handler = type(
        "ConfiguredLudoApiHandler",
        (LudoApiHandler,),
        {"database": database, "bot_token": bot_token},
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    return server
