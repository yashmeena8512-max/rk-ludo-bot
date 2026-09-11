# Ludo Telegram Bot

A Python Telegram bot starter for a Ludo game. It uses
[`python-telegram-bot`](https://docs.python-telegram-bot.org/) with long
polling.

## Current bot flow

- `/start` sends the RK Ludo welcome message in English and Hindi.
- **Play Ludo** opens **Create Game** and **Join Game** options.
- **Create Game** generates a unique six-character room and waits for one opponent.
- **Join Game** accepts a six-character room ID from another player.
- Once two players join, the bot assigns red and blue, chooses the first turn, and
  starts the game.
- During a turn, the server validates dice rolls and shows only legal token
  buttons. Tokens leave base on a six, can capture on non-safe squares, and
  finish at the end of the home path.
- **Profile** shows the player's name, username, virtual coins, games, wins,
  and losses.
- **Leaderboard** shows the top ten players by wins.

## Persistence

Game state is stored in `ludo.sqlite3` by default. Set `LUDO_DB_PATH` to use a
different SQLite file. The database contains separate tables for players,
games, game players, and game tokens. State transitions run in SQLite
transactions so callback data cannot bypass turn or membership checks.

The bot can restore a player's waiting or active game from `/start`, `/ludo`,
or the database after the Telegram client is closed.

The menu is built with inline buttons, so the next game features can reuse the
same callback-query flow.

## Run locally

The bot token must be available as `TELEGRAM_BOT_TOKEN`. Keep it in Replit
Secrets or another secure environment-variable store.

```bash
uv run python -m ludo_bot
```

You can also run the root entrypoint:

```bash
uv run python main.py
```

## Tests

Run the deterministic game-flow tests with:

```bash
uv run python -m unittest discover -s tests -v
```