# Ludo Telegram Bot

A Python Telegram bot starter for a Ludo game. Players can start the bot, open the Ludo menu, view their profile, and see the leaderboard entry point.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string
- `uv run python -m ludo_bot` — run the Telegram bot
- Required secret: `TELEGRAM_BOT_TOKEN` — the token issued by BotFather

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)
- Bot: Python 3.13 + `python-telegram-bot`

## Where things live

- `ludo_bot/main.py` — secret loading, database startup, and polling entrypoint
- `ludo_bot/bot.py` — Telegram commands, callback queries, and game messages
- `ludo_bot/database.py` — SQLite schema and atomic game-state transitions
- `ludo_bot/ludo_engine.py` — pure token movement, safe-square, and capture rules
- `ludo_bot/README.md` — bot behavior and local run instructions
- `tests/test_ludo_game.py` — deterministic room, turn, capture, win, and reconnect tests
- `pyproject.toml` — Python dependency and console entrypoint

## Architecture decisions

- All game state is stored in SQLite; Telegram callback messages are only a UI surface.
- SQLite `BEGIN IMMEDIATE` transactions serialize room, roll, move, and finish transitions.
- The first playable release uses inline callback buttons; a Mini App can replace the token-selection UI later without changing the game service.
- Telegram credentials are read from Replit Secrets at runtime and never stored in source.
- The bot uses polling for the initial release; matchmaking and persistent game state are intentionally follow-up work.

## Product

- `/start`, `/ludo`, `/profile`, `/leaderboard`, and `/cancel` are supported.
- Two players can create or join six-character rooms, roll server-side dice, move four tokens, capture, and finish a game.
- Profile and leaderboard stats are persisted in SQLite using virtual coins only.

## User preferences

- Use Python and `python-telegram-bot` for the bot runtime.

## Gotchas

- Keep HTTP client logging at warning level because Telegram Bot API request URLs contain the bot token.
- Validate room membership, turn ownership, dice state, and legal token moves inside the database transaction; never trust callback data.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
