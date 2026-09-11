"""SQLite persistence and atomic operations for RK Ludo games."""

from __future__ import annotations

import random
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ludo_engine import FINAL_POSITION, IllegalMove, legal_token_indexes, resolve_move

WAITING = "waiting"
PLAYING = "playing"
FINISHED = "finished"
CANCELLED = "cancelled"
ACTIVE_STATUSES = (WAITING, PLAYING)
ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class GameError(ValueError):
    """A safe, user-facing game state or validation error."""


@dataclass(frozen=True)
class PlayerProfile:
    telegram_id: int
    first_name: str
    username: str | None
    coins: int
    games_played: int
    wins: int
    losses: int


@dataclass(frozen=True)
class GamePlayer:
    telegram_id: int
    first_name: str
    username: str | None
    slot: int
    color: str


@dataclass
class GameState:
    room_id: str
    status: str
    current_turn_id: int | None
    dice_result: int | None
    rolled: bool
    winner_id: int | None
    players: list[GamePlayer]
    tokens: dict[int, list[int]]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RollOutcome:
    state: GameState
    dice: int
    legal_tokens: tuple[int, ...]
    passed: bool


@dataclass(frozen=True)
class MoveOutcome:
    state: GameState
    captured_tokens: tuple[tuple[int, int], ...]
    extra_turn: bool


class LudoDatabase:
    """Owns the SQLite connection and serializes every state transition."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> None:
        self.path = Path(path or "ludo.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        self._rng = rng or random.SystemRandom()
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    telegram_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    username TEXT,
                    coins INTEGER NOT NULL DEFAULT 1000,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS games (
                    room_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('waiting', 'playing', 'finished', 'cancelled')),
                    current_turn_id INTEGER,
                    dice_result INTEGER,
                    rolled INTEGER NOT NULL DEFAULT 0 CHECK (rolled IN (0, 1)),
                    winner_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS game_players (
                    room_id TEXT NOT NULL REFERENCES games(room_id) ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL REFERENCES players(telegram_id),
                    slot INTEGER NOT NULL CHECK (slot IN (1, 2)),
                    color TEXT NOT NULL CHECK (color IN ('red', 'blue')),
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY (room_id, telegram_id),
                    UNIQUE (room_id, slot)
                );

                CREATE TABLE IF NOT EXISTS game_tokens (
                    room_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    token_index INTEGER NOT NULL CHECK (token_index BETWEEN 0 AND 3),
                    progress INTEGER NOT NULL DEFAULT -1 CHECK (progress BETWEEN -1 AND 57),
                    finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
                    PRIMARY KEY (room_id, telegram_id, token_index),
                    FOREIGN KEY (room_id, telegram_id)
                        REFERENCES game_players(room_id, telegram_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_game_players_user
                    ON game_players(telegram_id);
                CREATE INDEX IF NOT EXISTS idx_games_status
                    ON games(status);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_player(
        self, telegram_id: int, first_name: str, username: str | None
    ) -> PlayerProfile:
        with self._transaction() as connection:
            now = self._now()
            connection.execute(
                """
                INSERT INTO players (
                    telegram_id, first_name, username, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (telegram_id, first_name, username, now, now),
            )
            return self._profile_from_row(
                connection.execute(
                    "SELECT * FROM players WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()
            )

    def get_profile(self, telegram_id: int) -> PlayerProfile | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM players WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return self._profile_from_row(row) if row else None

    def get_leaderboard(self, limit: int = 10) -> list[PlayerProfile]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM players
                ORDER BY wins DESC, games_played ASC, first_name ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._profile_from_row(row) for row in rows]

    def get_active_game_for_player(self, telegram_id: int) -> GameState | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT gp.room_id
                FROM game_players gp
                JOIN games g ON g.room_id = gp.room_id
                WHERE gp.telegram_id = ? AND g.status IN ('waiting', 'playing')
                ORDER BY g.updated_at DESC
                LIMIT 1
                """,
                (telegram_id,),
            ).fetchone()
            return self._load_game(self._connection, row["room_id"]) if row else None

    def create_game(
        self, telegram_id: int, first_name: str, username: str | None
    ) -> GameState:
        with self._transaction() as connection:
            self._upsert_player(connection, telegram_id, first_name, username)
            if self._active_game_id(connection, telegram_id):
                raise GameError("You already have an active game.")

            room_id = self._new_room_id(connection)
            now = self._now()
            connection.execute(
                """
                INSERT INTO games (
                    room_id, status, current_turn_id, rolled, created_at, updated_at
                ) VALUES (?, 'waiting', NULL, 0, ?, ?)
                """,
                (room_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO game_players (
                    room_id, telegram_id, slot, color, joined_at
                ) VALUES (?, ?, 1, 'red', ?)
                """,
                (room_id, telegram_id, now),
            )
            self._create_tokens(connection, room_id, telegram_id)
            return self._load_game(connection, room_id)

    def join_game(
        self, room_id: str, telegram_id: int, first_name: str, username: str | None
    ) -> GameState:
        normalized_room = room_id.strip().upper()
        with self._transaction() as connection:
            self._upsert_player(connection, telegram_id, first_name, username)
            game = self._row_for_game(connection, normalized_room)
            if game is None:
                raise GameError("That room does not exist.")
            if game["status"] != WAITING:
                raise GameError("That room is full or no longer available.")
            existing = connection.execute(
                """
                SELECT 1 FROM game_players
                WHERE room_id = ? AND telegram_id = ?
                """,
                (normalized_room, telegram_id),
            ).fetchone()
            if existing:
                raise GameError("You cannot join your own game.")
            if self._active_game_id(connection, telegram_id):
                raise GameError("Finish your current game before joining another one.")

            player_count = connection.execute(
                "SELECT COUNT(*) AS count FROM game_players WHERE room_id = ?",
                (normalized_room,),
            ).fetchone()["count"]
            if player_count >= 2:
                raise GameError("That room is already full.")

            now = self._now()
            connection.execute(
                """
                INSERT INTO game_players (
                    room_id, telegram_id, slot, color, joined_at
                ) VALUES (?, ?, 2, 'blue', ?)
                """,
                (normalized_room, telegram_id, now),
            )
            self._create_tokens(connection, normalized_room, telegram_id)
            creator = connection.execute(
                """
                SELECT telegram_id FROM game_players
                WHERE room_id = ? AND slot = 1
                """,
                (normalized_room,),
            ).fetchone()["telegram_id"]
            first_turn = self._rng.choice([creator, telegram_id])
            connection.execute(
                """
                UPDATE games
                SET status = 'playing', current_turn_id = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (first_turn, now, normalized_room),
            )
            return self._load_game(connection, normalized_room)

    def cancel_game(self, room_id: str, telegram_id: int) -> GameState:
        with self._transaction() as connection:
            game = self._require_player_game(connection, room_id, telegram_id)
            if game["status"] != WAITING:
                raise GameError("Only a waiting game can be cancelled.")
            player_count = connection.execute(
                "SELECT COUNT(*) AS count FROM game_players WHERE room_id = ?",
                (room_id,),
            ).fetchone()["count"]
            if player_count != 1:
                raise GameError("This game has already started.")
            now = self._now()
            connection.execute(
                "UPDATE games SET status = 'cancelled', updated_at = ? WHERE room_id = ?",
                (now, room_id),
            )
            return self._load_game(connection, room_id)

    def leave_game(self, room_id: str, telegram_id: int) -> GameState:
        with self._transaction() as connection:
            game = self._require_player_game(connection, room_id, telegram_id)
            if game["status"] == WAITING:
                now = self._now()
                connection.execute(
                    "UPDATE games SET status = 'cancelled', updated_at = ? WHERE room_id = ?",
                    (now, room_id),
                )
                return self._load_game(connection, room_id)
            if game["status"] != PLAYING:
                raise GameError("That game is no longer active.")

            opponent = connection.execute(
                """
                SELECT telegram_id FROM game_players
                WHERE room_id = ? AND telegram_id != ?
                """,
                (room_id, telegram_id),
            ).fetchone()
            if opponent is None:
                raise GameError("There is no opponent in this game.")
            self._finish_game(connection, room_id, opponent["telegram_id"])
            return self._load_game(connection, room_id)

    def roll_dice(self, room_id: str, telegram_id: int) -> RollOutcome:
        with self._transaction() as connection:
            game = self._require_player_game(connection, room_id, telegram_id)
            if game["status"] != PLAYING:
                raise GameError("This game is not active.")
            if game["current_turn_id"] != telegram_id:
                raise GameError("It is not your turn.")
            if game["rolled"]:
                raise GameError("You already rolled. Choose a token to move.")

            dice = self._rng.randint(1, 6)
            player_tokens = [
                row["progress"]
                for row in connection.execute(
                    """
                    SELECT progress FROM game_tokens
                    WHERE room_id = ? AND telegram_id = ?
                    ORDER BY token_index
                    """,
                    (room_id, telegram_id),
                ).fetchall()
            ]
            legal = legal_token_indexes(player_tokens, dice)
            now = self._now()
            if legal:
                connection.execute(
                    """
                    UPDATE games
                    SET dice_result = ?, rolled = 1, updated_at = ?
                    WHERE room_id = ?
                    """,
                    (dice, now, room_id),
                )
                state = self._load_game(connection, room_id)
                return RollOutcome(state, dice, legal, False)

            next_player = self._next_player_id(connection, room_id, telegram_id)
            connection.execute(
                """
                UPDATE games
                SET dice_result = NULL, rolled = 0, current_turn_id = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (next_player, now, room_id),
            )
            state = self._load_game(connection, room_id)
            return RollOutcome(state, dice, (), True)

    def move_token(
        self, room_id: str, telegram_id: int, token_index: int
    ) -> MoveOutcome:
        with self._transaction() as connection:
            game = self._require_player_game(connection, room_id, telegram_id)
            if game["status"] != PLAYING:
                raise GameError("This game is not active.")
            if game["current_turn_id"] != telegram_id:
                raise GameError("It is not your turn.")
            if not game["rolled"] or game["dice_result"] is None:
                raise GameError("Roll the dice before choosing a token.")
            if token_index not in range(4):
                raise GameError("That token does not exist.")

            state = self._load_game(connection, room_id)
            player = next(
                player for player in state.players if player.telegram_id == telegram_id
            )
            opponent_data = {
                opponent.telegram_id: (
                    opponent.color,
                    state.tokens[opponent.telegram_id],
                )
                for opponent in state.players
            }
            try:
                resolution = resolve_move(
                    player_id=telegram_id,
                    color=player.color,
                    token_index=token_index,
                    tokens=state.tokens[telegram_id],
                    dice=game["dice_result"],
                    opponents=opponent_data,
                )
            except IllegalMove as error:
                raise GameError(str(error)) from error

            now = self._now()
            connection.execute(
                """
                UPDATE game_tokens
                SET progress = ?, finished = ?
                WHERE room_id = ? AND telegram_id = ? AND token_index = ?
                """,
                (
                    resolution.new_progress,
                    int(resolution.new_progress == FINAL_POSITION),
                    room_id,
                    telegram_id,
                    token_index,
                ),
            )
            for opponent_id, opponent_index in resolution.captured_tokens:
                connection.execute(
                    """
                    UPDATE game_tokens
                    SET progress = -1, finished = 0
                    WHERE room_id = ? AND telegram_id = ? AND token_index = ?
                    """,
                    (room_id, opponent_id, opponent_index),
                )

            updated_tokens = list(state.tokens[telegram_id])
            updated_tokens[token_index] = resolution.new_progress
            all_finished = all(token == FINAL_POSITION for token in updated_tokens)
            if all_finished:
                self._finish_game(connection, room_id, telegram_id)
                return MoveOutcome(
                    self._load_game(connection, room_id),
                    resolution.captured_tokens,
                    False,
                )

            dice = game["dice_result"]
            extra_turn = dice == 6
            next_player = (
                telegram_id
                if extra_turn
                else self._next_player_id(connection, room_id, telegram_id)
            )
            connection.execute(
                """
                UPDATE games
                SET dice_result = NULL, rolled = 0, current_turn_id = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (next_player, now, room_id),
            )
            return MoveOutcome(
                self._load_game(connection, room_id),
                resolution.captured_tokens,
                extra_turn,
            )

    def _create_schema_if_needed(self) -> None:
        self._create_schema()

    def _profile_from_row(self, row: sqlite3.Row) -> PlayerProfile:
        return PlayerProfile(
            telegram_id=row["telegram_id"],
            first_name=row["first_name"],
            username=row["username"],
            coins=row["coins"],
            games_played=row["games_played"],
            wins=row["wins"],
            losses=row["losses"],
        )

    def _upsert_player(
        self,
        connection: sqlite3.Connection,
        telegram_id: int,
        first_name: str,
        username: str | None,
    ) -> None:
        now = self._now()
        connection.execute(
            """
            INSERT INTO players (
                telegram_id, first_name, username, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                updated_at = excluded.updated_at
            """,
            (telegram_id, first_name, username, now, now),
        )

    def _new_room_id(self, connection: sqlite3.Connection) -> str:
        for _ in range(100):
            room_id = "".join(self._rng.choice(ROOM_ALPHABET) for _ in range(6))
            if not self._row_for_game(connection, room_id):
                return room_id
        raise GameError("Could not create a unique room. Please try again.")

    @staticmethod
    def _create_tokens(
        connection: sqlite3.Connection, room_id: str, telegram_id: int
    ) -> None:
        connection.executemany(
            """
            INSERT INTO game_tokens (room_id, telegram_id, token_index)
            VALUES (?, ?, ?)
            """,
            [(room_id, telegram_id, index) for index in range(4)],
        )

    @staticmethod
    def _row_for_game(
        connection: sqlite3.Connection, room_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM games WHERE room_id = ?", (room_id,)
        ).fetchone()

    @staticmethod
    def _active_game_id(
        connection: sqlite3.Connection, telegram_id: int
    ) -> str | None:
        row = connection.execute(
            """
            SELECT gp.room_id
            FROM game_players gp
            JOIN games g ON g.room_id = gp.room_id
            WHERE gp.telegram_id = ? AND g.status IN ('waiting', 'playing')
            LIMIT 1
            """,
            (telegram_id,),
        ).fetchone()
        return row["room_id"] if row else None

    def _require_player_game(
        self, connection: sqlite3.Connection, room_id: str, telegram_id: int
    ) -> sqlite3.Row:
        game = self._row_for_game(connection, room_id)
        if game is None:
            raise GameError("That game room does not exist.")
        member = connection.execute(
            """
            SELECT 1 FROM game_players
            WHERE room_id = ? AND telegram_id = ?
            """,
            (room_id, telegram_id),
        ).fetchone()
        if member is None:
            raise GameError("You are not a player in that game.")
        return game

    @staticmethod
    def _next_player_id(
        connection: sqlite3.Connection, room_id: str, telegram_id: int
    ) -> int:
        row = connection.execute(
            """
            SELECT telegram_id FROM game_players
            WHERE room_id = ? AND telegram_id != ?
            ORDER BY slot
            LIMIT 1
            """,
            (room_id, telegram_id),
        ).fetchone()
        if row is None:
            raise GameError("Your opponent is no longer in this game.")
        return row["telegram_id"]

    def _finish_game(
        self, connection: sqlite3.Connection, room_id: str, winner_id: int
    ) -> None:
        now = self._now()
        players = connection.execute(
            "SELECT telegram_id FROM game_players WHERE room_id = ?", (room_id,)
        ).fetchall()
        connection.execute(
            """
            UPDATE games
            SET status = 'finished', winner_id = ?, current_turn_id = NULL,
                dice_result = NULL, rolled = 0, updated_at = ?
            WHERE room_id = ?
            """,
            (winner_id, now, room_id),
        )
        for player in players:
            telegram_id = player["telegram_id"]
            if telegram_id == winner_id:
                connection.execute(
                    """
                    UPDATE players
                    SET games_played = games_played + 1, wins = wins + 1,
                        updated_at = ?
                    WHERE telegram_id = ?
                    """,
                    (now, telegram_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE players
                    SET games_played = games_played + 1, losses = losses + 1,
                        updated_at = ?
                    WHERE telegram_id = ?
                    """,
                    (now, telegram_id),
                )

    def _load_game(
        self, connection: sqlite3.Connection, room_id: str
    ) -> GameState:
        row = self._row_for_game(connection, room_id)
        if row is None:
            raise GameError("That game room does not exist.")
        players = [
            GamePlayer(
                telegram_id=player["telegram_id"],
                first_name=player["first_name"],
                username=player["username"],
                slot=player["slot"],
                color=player["color"],
            )
            for player in connection.execute(
                """
                SELECT gp.*, p.first_name, p.username
                FROM game_players gp
                JOIN players p ON p.telegram_id = gp.telegram_id
                WHERE gp.room_id = ?
                ORDER BY gp.slot
                """,
                (room_id,),
            ).fetchall()
        ]
        tokens = {
            player.telegram_id: [
                token["progress"]
                for token in connection.execute(
                    """
                    SELECT progress FROM game_tokens
                    WHERE room_id = ? AND telegram_id = ?
                    ORDER BY token_index
                    """,
                    (room_id, player.telegram_id),
                ).fetchall()
            ]
            for player in players
        }
        return GameState(
            room_id=row["room_id"],
            status=row["status"],
            current_turn_id=row["current_turn_id"],
            dice_result=row["dice_result"],
            rolled=bool(row["rolled"]),
            winner_id=row["winner_id"],
            players=players,
            tokens=tokens,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
