import logging
import os
from threading import Thread

from bot import build_application
from database import LudoDatabase
from mini_api import start_api_server

TOKEN_ENV_VAR = "BOT_TOKEN"


def main() -> None:
    """Start the bot with long polling and a persistent SQLite database."""
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    # Bot API request URLs contain the token, so keep HTTP client URLs out of logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is required. Add the token securely before starting the bot."
        )

    database = LudoDatabase(os.environ.get("LUDO_DB_PATH"))
    api_server = start_api_server(
        database,
        token,
        int(os.environ.get("PORT", os.environ.get("LUDO_API_PORT", "10000"))),

    )
    api_thread = Thread(
        target=api_server.serve_forever,
        name="ludo-mini-app-api",
        daemon=True,
    )
    api_thread.start()
    application = build_application(database, token)
    try:
        application.run_polling()
    finally:
        api_server.shutdown()
        api_server.server_close()
if __name__ == "__main__":
    main()
