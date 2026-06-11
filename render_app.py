from __future__ import annotations

import os
import threading

from app import app, restore_jobs_state


def telegram_enabled() -> bool:
    return (
        os.getenv("TELEGRAM_BOT_ENABLED", "true").lower()
        in {"1", "true", "yes"}
        and bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    )


def start_telegram_bot() -> threading.Thread | None:
    if not telegram_enabled():
        return None

    from telegram_bot import run_bot

    thread = threading.Thread(
        target=run_bot,
        name="reelposter-telegram",
        daemon=True,
    )
    thread.start()
    return thread


restore_jobs_state()
bot_thread = start_telegram_bot()
app.config["TELEGRAM_BOT_THREAD"] = bot_thread
application = app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
