"""Pure Ludo movement rules used by the database-backed game service."""

from dataclasses import dataclass

BASE_POSITION = -1
TRACK_LENGTH = 52
TRACK_END = TRACK_LENGTH - 1
FINAL_POSITION = 57
SIX_TO_LEAVE_BASE = 6

# These positions represent the common star/safe squares on the shared track.
# A token on one of these squares cannot be captured.
SAFE_POSITIONS = frozenset({0, 8, 13, 21, 26, 34, 39, 47})
COLOR_OFFSETS = {"red": 0, "blue": 26}


class IllegalMove(ValueError):
    """Raised when a player attempts a move that Ludo does not allow."""


@dataclass(frozen=True)
class MoveResolution:
    new_progress: int
    captured_tokens: tuple[tuple[int, int], ...]


def legal_token_indexes(tokens: list[int] | tuple[int, ...], dice: int) -> tuple[int, ...]:
    """Return the tokens that can legally move for a dice value."""
    if dice not in range(1, 7):
        raise ValueError("Dice value must be between 1 and 6.")

    legal: list[int] = []
    for index, progress in enumerate(tokens):
        if progress == FINAL_POSITION:
            continue
        if progress == BASE_POSITION:
            if dice == SIX_TO_LEAVE_BASE:
                legal.append(index)
            continue
        if progress + dice <= FINAL_POSITION:
            legal.append(index)
    return tuple(legal)


def absolute_position(color: str, progress: int) -> int | None:
    """Map a player's shared-track progress to a board square."""
    if progress < 0 or progress > TRACK_END:
        return None
    if color not in COLOR_OFFSETS:
        raise ValueError(f"Unsupported player color: {color}")
    return (COLOR_OFFSETS[color] + progress) % TRACK_LENGTH


def resolve_move(
    *,
    player_id: int,
    color: str,
    token_index: int,
    tokens: list[int] | tuple[int, ...],
    dice: int,
    opponents: dict[int, tuple[str, list[int] | tuple[int, ...]]],
) -> MoveResolution:
    """Apply one move and calculate captures without mutating stored state."""
    legal = legal_token_indexes(tokens, dice)
    if token_index not in legal:
        raise IllegalMove("That token cannot move with the current dice roll.")

    current = tokens[token_index]
    new_progress = 0 if current == BASE_POSITION else current + dice
    landing = absolute_position(color, new_progress)
    captured: list[tuple[int, int]] = []

    if landing is not None and landing not in SAFE_POSITIONS:
        for opponent_id, (opponent_color, opponent_tokens) in opponents.items():
            if opponent_id == player_id:
                continue
            for opponent_index, opponent_progress in enumerate(opponent_tokens):
                if absolute_position(opponent_color, opponent_progress) == landing:
                    captured.append((opponent_id, opponent_index))

    return MoveResolution(new_progress=new_progress, captured_tokens=tuple(captured))