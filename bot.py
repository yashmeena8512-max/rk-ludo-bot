"""Telegram handlers for the server-side, two-player RK Ludo game."""

from __future__ import annotations

import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import (
    CANCELLED,
    FINISHED,
    PLAYING,
    WAITING,
    GameError,
    GameState,
    LudoDatabase,
    PlayerProfile,
)
from ludo_engine import BASE_POSITION, FINAL_POSITION, legal_token_indexes

logger = logging.getLogger(__name__)
ROOM_PATTERN = re.compile(r"^[A-Z0-9]{6}$")
COLOR_EMOJIS = {"red": "🔴", "blue": "🔵"}


def mini_app_url() -> str:
    """Resolve the public Mini App URL without embedding a deployment domain."""
    configured = os.environ.get("LUDO_MINI_APP_URL")
    if configured:
        return configured.rstrip("/") + "/"
    domains = os.environ.get("REPLIT_DOMAINS", "")
    domain = domains.split(",")[0].strip() or os.environ.get("REPLIT_DEV_DOMAIN", "")
    if domain:
        return f"https://{domain.rstrip('/')}/"
    raise RuntimeError(
        "LUDO_MINI_APP_URL or a Replit domain is required for the Play Ludo button."
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎲 Play Ludo",
                    web_app=WebAppInfo(url=mini_app_url()),
                )
            ],
            [
                InlineKeyboardButton("👤 Profile", callback_data="profile"),
                InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
            ],
        ]
    )


def game_options_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🆕 Create Game", callback_data="create_game")],
            [InlineKeyboardButton("🔗 Join Game", callback_data="join_game")],
        ]
    )


def waiting_game_menu(room_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel Game", callback_data=f"cancel:{room_id}")]]
    )


def game_menu(
    state: GameState,
    viewer_id: int,
    legal_tokens: tuple[int, ...] = (),
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if state.status == WAITING:
        buttons.append(
            [InlineKeyboardButton("❌ Cancel Game", callback_data=f"cancel:{state.room_id}")]
        )
    elif state.status == PLAYING:
        if state.current_turn_id == viewer_id:
            if state.rolled:
                for token_index in legal_tokens:
                    player = next(
                        player
                        for player in state.players
                        if player.telegram_id == viewer_id
                    )
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                f"{COLOR_EMOJIS[player.color]} Token {token_index + 1}",
                                callback_data=f"move:{state.room_id}:{token_index}",
                            )
                        ]
                    )
            else:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            "🎲 Roll Dice", callback_data=f"roll:{state.room_id}"
                        )
                    ]
                )
        buttons.append(
            [InlineKeyboardButton("❌ Leave Game", callback_data=f"leave:{state.room_id}")]
        )
    return InlineKeyboardMarkup(buttons) if buttons else main_menu()


def player_name(update: Update) -> str:
    user = update.effective_user
    return user.first_name if user and user.first_name else "Player"


def profile_from_update(update: Update) -> tuple[int, str, str | None]:
    user = update.effective_user
    if user is None:
        raise GameError("Telegram user information is unavailable.")
    return user.id, user.first_name or "Player", user.username


def profile_text(profile: PlayerProfile) -> str:
    username = f"@{profile.username}" if profile.username else "Not set"
    return (
        "👤 Your Profile\n"
        f"Name: {profile.first_name}\n"
        f"Username: {username}\n"
        f"Coins: {profile.coins}\n"
        f"Games: {profile.games_played}\n"
        f"Wins: {profile.wins}\n"
        f"Losses: {profile.losses}"
    )


def leaderboard_text(players: list[PlayerProfile]) -> str:
    if not players:
        return "🏆 Leaderboard\n\nNo players on the leaderboard yet."
    lines = ["🏆 Leaderboard", ""]
    for index, player in enumerate(players, start=1):
        lines.append(f"{index}. {player.first_name} — {player.wins} wins")
    return "\n".join(lines)


def _token_state(progress: int) -> str:
    if progress == BASE_POSITION:
        return "Base"
    if progress == FINAL_POSITION:
        return "Home"
    return str(progress)


def game_text(
    state: GameState,
    viewer_id: int,
    *,
    note: str | None = None,
) -> str:
    lines = [f"🎮 Ludo Game  •  Room: {state.room_id}", ""]
    for player in state.players:
        icon = COLOR_EMOJIS[player.color]
        tokens = ", ".join(
            f"T{index + 1}: {_token_state(progress)}"
            for index, progress in enumerate(state.tokens[player.telegram_id])
        )
        lines.append(f"{icon} {player.first_name} ({player.color.title()})")
        lines.append(f"   {tokens}")

    if state.status == WAITING:
        lines.extend(["", "Waiting for Player 2..."])
    elif state.status == PLAYING:
        current = next(
            player
            for player in state.players
            if player.telegram_id == state.current_turn_id
        )
        if state.current_turn_id == viewer_id:
            turn_text = "Your turn"
        else:
            turn_text = f"{current.first_name}'s turn"
        lines.extend(["", f"Turn: {turn_text}"])
        if state.dice_result is not None:
            lines.append(f"🎲 Dice result: {state.dice_result}")
    elif state.status == FINISHED:
        winner = next(
            player for player in state.players if player.telegram_id == state.winner_id
        )
        lines.extend(["", f"🏆 {winner.first_name} won the game!"])
    elif state.status == CANCELLED:
        lines.extend(["", "This game was cancelled."])

    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def _legal_tokens_for_viewer(state: GameState, viewer_id: int) -> tuple[int, ...]:
    if not state.rolled or state.current_turn_id != viewer_id or state.dice_result is None:
        return ()
    return legal_token_indexes(state.tokens[viewer_id], state.dice_result)


async def _edit_query(
    query, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as error:
        if "Message is not modified" not in str(error):
            raise


async def _broadcast_game(
    bot,
    state: GameState,
    *,
    current_query=None,
    notes: dict[int, str] | None = None,
) -> None:
    notes = notes or {}
    current_user_id = current_query.from_user.id if current_query else None
    for player in state.players:
        text = game_text(state, player.telegram_id, note=notes.get(player.telegram_id))
        markup = game_menu(
            state,
            player.telegram_id,
            _legal_tokens_for_viewer(state, player.telegram_id),
        )
        if player.telegram_id == current_user_id:
            await _edit_query(current_query, text, markup)
        else:
            await bot.send_message(
                chat_id=player.telegram_id,
                text=text,
                reply_markup=markup,
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id, first_name, username = profile_from_update(update)
    database: LudoDatabase = context.application.bot_data["database"]
    database.upsert_player(telegram_id, first_name, username)
    if not update.message:
        return

    await update.message.reply_text(
        "🎮 Welcome to RK Ludo!\n"
        "यहाँ आप दोस्तों के साथ Ludo खेल सकते हैं।",
        reply_markup=main_menu(),
    )
    active_game = database.get_active_game_for_player(telegram_id)
    if active_game:
        await update.message.reply_text(
            "Your active game has been restored.\n\n"
            + game_text(active_game, telegram_id),
            reply_markup=game_menu(
                active_game,
                telegram_id,
                _legal_tokens_for_viewer(active_game, telegram_id),
            ),
        )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id, first_name, username = profile_from_update(update)
    database: LudoDatabase = context.application.bot_data["database"]
    profile = database.upsert_player(telegram_id, first_name, username)
    if update.message:
        await update.message.reply_text(profile_text(profile), reply_markup=main_menu())


async def ludo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id, first_name, username = profile_from_update(update)
    database: LudoDatabase = context.application.bot_data["database"]
    database.upsert_player(telegram_id, first_name, username)
    if not update.message:
        return
    active_game = database.get_active_game_for_player(telegram_id)
    if active_game:
        await update.message.reply_text(
            game_text(active_game, telegram_id),
            reply_markup=game_menu(
                active_game,
                telegram_id,
                _legal_tokens_for_viewer(active_game, telegram_id),
            ),
        )
    else:
        await update.message.reply_text(
            "🎮 Ludo Game\nChoose an option:", reply_markup=game_options_menu()
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_room_id", None)
    telegram_id, _, _ = profile_from_update(update)
        database: LudoDatabase = context.application.bot_data["database"]
    active_game = database.get_active_game_for_player(telegram_id)
    if not update.message:
        return
    if not active_game:
        await update.message.reply_text("Nothing to cancel.", reply_markup=main_menu())
        return
    try:
        if active_game.status == WAITING:
            state = database.cancel_game(active_game.room_id, telegram_id)
            message = f"Game {state.room_id} cancelled."
        else:
            state = database.leave_game(active_game.room_id, telegram_id)
            message = "You left the game."
        await update.message.reply_text(message, reply_markup=main_menu())
    except GameError as error:
        await update.message.reply_text(str(error), reply_markup=main_menu())


async def join_room_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.pop("awaiting_room_id", False):
        return
    if not update.message or not update.message.text:
        return
    room_id = update.message.text.strip().upper()
    if not ROOM_PATTERN.fullmatch(room_id):
        await update.message.reply_text("Send the 6-character room ID.")
        context.user_data["awaiting_room_id"] = True
        return

    telegram_id, first_name, username = profile_from_update(update)
    database: LudoDatabase = database: LudoDatabase = context.application.bot_data["database"]
    try:
        state = database.join_game(room_id, telegram_id, first_name, username)
    except GameError as error:
        await update.message.reply_text(str(error), reply_markup=main_menu())
        return

    await update.message.reply_text(
        game_text(state, telegram_id),
        reply_markup=game_menu(
            state, telegram_id, _legal_tokens_for_viewer(state, telegram_id)
        ),
    )
    for player in state.players:
        if player.telegram_id != telegram_id:
            await context.bot.send_message(
                chat_id=player.telegram_id,
                text=game_text(state, player.telegram_id),
                reply_markup=game_menu(
                    state,
                    player.telegram_id,
                    _legal_tokens_for_viewer(state, player.telegram_id),
                ),
            )


async def handle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    database: LudoDatabase = database: LudoDatabase = context.application.bot_data["database"]
    telegram_id, first_name, username = profile_from_update(update)
    database.upsert_player(telegram_id, first_name, username)
    data = query.data or ""

    try:
        if data == "play_ludo":
            await _edit_query(
                query, "🎮 Ludo Game\nChoose an option:", game_options_menu()
            )
        elif data == "join_game":
            context.user_data["awaiting_room_id"] = True
            await _edit_query(query, "Send the 6-character room ID to join:", main_menu())
        elif data == "create_game":
            state = database.create_game(telegram_id, first_name, username)
            await _edit_query(
                query,
                f"{game_text(state, telegram_id)}\n\nRoom ID: {state.room_id}",
                waiting_game_menu(state.room_id),
            )
        elif data == "profile":
            profile = database.get_profile(telegram_id)
            if profile is None:
                raise GameError("Profile is not available yet.")
            await _edit_query(query, profile_text(profile), main_menu())
        elif data == "leaderboard":
            await _edit_query(
                query, leaderboard_text(database.get_leaderboard()), main_menu()
            )
        elif data.startswith(("cancel:", "leave:", "roll:", "move:")):
            await handle_game_callback(query, context, database, data, telegram_id)
        else:
            await _edit_query(query, "Choose an option from the menu below.", main_menu())
    except GameError as error:
        await query.answer(str(error), show_alert=True)


async def handle_game_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    database: LudoDatabase,
    data: str,
    telegram_id: int,
) -> None:
    parts = data.split(":")
    room_id = parts[1] if len(parts) > 1 else ""
    if not ROOM_PATTERN.fullmatch(room_id):
        raise GameError("Invalid game room.")

    if parts[0] == "cancel":
        state = database.cancel_game(room_id, telegram_id)
        await _edit_query(query, f"Game {state.room_id} cancelled.", main_menu())
        return

    if parts[0] == "leave":
        state = database.leave_game(room_id, telegram_id)
        await _broadcast_game(
            context.bot,
            state,
            current_query=query,
            notes={telegram_id: "You left the game."},
        )
        return

    if parts[0] == "roll":
        outcome = database.roll_dice(room_id, telegram_id)
        if outcome.passed:
            note = (
                f"🎲 You rolled {outcome.dice}, but no legal move was available. "
                "Your turn passes."
            )
        else:
            note = f"🎲 You rolled {outcome.dice}. Choose a legal token."
        await _broadcast_game(
            context.bot,
            outcome.state,
            current_query=query,
            notes={telegram_id: note},
        )
        return

    if parts[0] == "move":
        if len(parts) != 3 or not parts[2].isdigit():
            raise GameError("Invalid token selection.")
        outcome = database.move_token(room_id, telegram_id, int(parts[2]))
        notes = {}
        if outcome.captured_tokens:
            notes[telegram_id] = "You captured an opponent token!"
        if outcome.extra_turn:
            notes[telegram_id] = "You rolled a 6, so you get another turn."
        await _broadcast_game(
            context.bot,
            outcome.state,
            current_query=query,
            notes=notes,
        )


async def leaderboard_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: LudoDatabase = database: LudoDatabase = context.application.bot_data["database"]
    if update.message:
        await update.message.reply_text(
            leaderboard_text(database.get_leaderboard()), reply_markup=main_menu()
        )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled update error", exc_info=context.error)
    del update


async def announce_bot(application: Application) -> None:
    bot = await application.bot.get_me()
    logger.info("Ludo bot connected as @%s", bot.username or bot.first_name)


def build_application(database: LudoDatabase, token: str) -> Application:
    application = (
        Application.builder()
        .token(token)
        .post_init(announce_bot)
        .build()
    )
    application.bot_data["database"] = database
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("ludo", ludo_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, join_room_text)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(handle_error)
    return application
