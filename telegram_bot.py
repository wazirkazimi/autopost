from __future__ import annotations

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import app as reelposter


POSITION_PRESETS = {
    "tl": ("Top left", 18.0, 15.0),
    "tr": ("Top right", 82.0, 15.0),
    "bl": ("Bottom left", 18.0, 85.0),
    "br": ("Bottom right", 82.0, 85.0),
}
STATUS_LABELS = {
    "queued": "Queued",
    "downloading": "Downloading",
    "ready": "Ready",
    "watermarking": "Watermarking",
    "uploading": "Uploading",
    "scheduled": "Scheduled",
    "posting": "Posting",
    "done": "Done",
    "error": "Error",
}


def allowed_user_ids() -> set[int]:
    result = set()
    for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(","):
        value = value.strip()
        if value.isdigit():
            result.add(int(value))
    return result


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in allowed_user_ids())


async def reject_unauthorized(update: Update) -> None:
    user_id = update.effective_user.id if update.effective_user else "unknown"
    text = (
        f"Your Telegram user ID is {user_id}.\n"
        "Add it to TELEGRAM_ALLOWED_USER_IDS on the server, then restart ReelPoster."
    )
    if update.callback_query:
        await update.callback_query.answer("This bot is private.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)


def chat_jobs(chat_id: int) -> list[dict]:
    with reelposter.jobs_lock:
        result = [
            dict(job)
            for job in reelposter.jobs.values()
            if job.get("telegram_chat_id") == chat_id
        ]
    return sorted(
        result,
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )


def current_job(chat_id: int) -> dict | None:
    items = chat_jobs(chat_id)
    for job in items:
        if job.get("status") in {
            "queued",
            "downloading",
            "ready",
            "watermarking",
            "uploading",
            "scheduled",
            "posting",
        }:
            return job
    return items[0] if items else None


def selected(label: str, active: bool) -> str:
    return f"[x] {label}" if active else label


def account_options() -> list[dict]:
    return [reelposter.public_account(item) for item in reelposter.account_profiles()]


def overlay_options() -> list[dict]:
    return [reelposter.public_overlay(item) for item in reelposter.overlay_profiles()]


def option_name(options: list[dict], option_id: str | None, fallback: str) -> str:
    option = next((item for item in options if item["id"] == option_id), None)
    return option["name"] if option else fallback


def next_option_id(options: list[dict], current_id: str | None) -> str | None:
    if not options:
        return None
    ids = [item["id"] for item in options]
    if current_id not in ids:
        return ids[0]
    return ids[(ids.index(current_id) + 1) % len(ids)]


def draft_keyboard(job: dict) -> InlineKeyboardMarkup:
    position = job.get("telegram_position", "br")
    size = int(job.get("telegram_size", 16))
    destination = job.get("telegram_destination", "grid")
    delay = int(job.get("telegram_delay", 0))
    scheduled = bool(job.get("telegram_scheduled_at"))
    hide_counts = bool(job.get("telegram_hide_counts", False))
    accounts = account_options()
    overlays = overlay_options()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Account: "
                    + option_name(
                        accounts,
                        job.get("telegram_account_id"),
                        "Not configured",
                    ),
                    callback_data="rp:acct:next",
                )
            ],
            [
                InlineKeyboardButton(
                    "Overlay: "
                    + option_name(
                        overlays,
                        job.get("telegram_overlay_id"),
                        "Not configured",
                    ),
                    callback_data="rp:over:next",
                )
            ],
            [
                InlineKeyboardButton(
                    selected("Top left", position == "tl"),
                    callback_data="rp:pos:tl",
                ),
                InlineKeyboardButton(
                    selected("Top right", position == "tr"),
                    callback_data="rp:pos:tr",
                ),
            ],
            [
                InlineKeyboardButton(
                    selected("Bottom left", position == "bl"),
                    callback_data="rp:pos:bl",
                ),
                InlineKeyboardButton(
                    selected("Bottom right", position == "br"),
                    callback_data="rp:pos:br",
                ),
            ],
            [
                InlineKeyboardButton(
                    selected("10%", size == 10),
                    callback_data="rp:size:10",
                ),
                InlineKeyboardButton(
                    selected("16%", size == 16),
                    callback_data="rp:size:16",
                ),
                InlineKeyboardButton(
                    selected("22%", size == 22),
                    callback_data="rp:size:22",
                ),
                InlineKeyboardButton(
                    selected("28%", size == 28),
                    callback_data="rp:size:28",
                ),
            ],
            [
                InlineKeyboardButton(
                    selected("Grid + Reels", destination == "grid"),
                    callback_data="rp:dest:grid",
                ),
                InlineKeyboardButton(
                    selected("Reels only", destination == "reels-only"),
                    callback_data="rp:dest:reels-only",
                ),
            ],
            [
                InlineKeyboardButton(
                    selected("Hide counts after post", hide_counts)
                    if hide_counts
                    else "Hide counts: Off",
                    callback_data="rp:counts:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    selected("Now", delay == 0 and not scheduled),
                    callback_data="rp:delay:0",
                ),
                InlineKeyboardButton(
                    selected("15m", delay == 15 and not scheduled),
                    callback_data="rp:delay:15",
                ),
                InlineKeyboardButton(
                    selected("30m", delay == 30 and not scheduled),
                    callback_data="rp:delay:30",
                ),
                InlineKeyboardButton(
                    selected("60m", delay == 60 and not scheduled),
                    callback_data="rp:delay:60",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Publish Reel",
                    callback_data="rp:publish:now",
                )
            ],
        ]
    )


def draft_summary(job: dict) -> str:
    position = POSITION_PRESETS[job.get("telegram_position", "br")][0]
    destination = (
        "Grid + Reels"
        if job.get("telegram_destination", "grid") == "grid"
        else "Reels only"
    )
    scheduled_at = job.get("telegram_scheduled_at")
    if scheduled_at:
        timing = f"Scheduled: {scheduled_at}"
    else:
        delay = int(job.get("telegram_delay", 0))
        timing = "Post now" if delay == 0 else f"Delay: {delay} minutes"
    account_name = option_name(
        account_options(),
        job.get("telegram_account_id"),
        "Not configured",
    )
    overlay_name = option_name(
        overlay_options(),
        job.get("telegram_overlay_id"),
        "Not configured",
    )
    count_setting = (
        "Requested (manual Instagram step)"
        if job.get("telegram_hide_counts")
        else "Off"
    )
    return (
        "Reel ready.\n"
        f"Account: {account_name}\n"
        f"Overlay: {overlay_name}\n"
        f"Placement: {position}, {int(job.get('telegram_size', 16))}%\n"
        f"Destination: {destination}\n"
        f"Hide counts: {count_setting}\n"
        f"{timing}\n\n"
        "Use /caption followed by new text to replace the caption.\n"
        "Use /schedule YYYY-MM-DD HH:MM for an exact local time."
    )


async def send_ready_controls(application: Application, job: dict) -> None:
    job = reelposter.get_job_or_404(job["id"])
    if not job or job.get("telegram_controls_sent"):
        return
    reelposter.update_job(job["id"], telegram_controls_sent=True)
    chat_id = job["telegram_chat_id"]
    caption = job.get("telegram_caption") or job.get("caption") or "(No caption)"
    caption_message = "Fetched caption:\n\n" + caption
    if len(caption_message) > 4096:
        caption_message = caption_message[:4080] + "\n[caption truncated]"
    try:
        await application.bot.send_message(chat_id=chat_id, text=caption_message)
        message = await application.bot.send_message(
            chat_id=chat_id,
            text=draft_summary(job),
            reply_markup=draft_keyboard(job),
        )
    except Exception:
        reelposter.update_job(job["id"], telegram_controls_sent=False)
        raise
    reelposter.update_job(
        job["id"],
        telegram_controls_message_id=message.message_id,
        telegram_caption=caption if caption != "(No caption)" else "",
    )


async def wait_for_prepare(application: Application, job_id: str) -> None:
    for _ in range(900):
        await asyncio.sleep(2)
        job = reelposter.get_job_or_404(job_id)
        if not job:
            return
        if job.get("status") == "ready":
            await send_ready_controls(application, job)
            return
        if job.get("status") == "error":
            await application.bot.send_message(
                chat_id=job["telegram_chat_id"],
                text=f"Reel preparation failed:\n{job.get('error') or job.get('message')}",
            )
            reelposter.update_job(
                job_id,
                telegram_terminal_notified=True,
                telegram_last_notified_status="error",
            )
            return


async def start_command(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user_id = update.effective_user.id if update.effective_user else "unknown"
    if not is_authorized(update):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(
        "ReelPoster is ready.\n"
        f"Your Telegram user ID is {user_id}.\n\n"
        "Send a public Instagram Reel, YouTube, Reddit, or X video URL to begin.\n"
        "By sending a URL, you confirm that you own the video or have permission "
        "to repost it. Commands:\n"
        "/caption NEW TEXT\n"
        "/schedule YYYY-MM-DD HH:MM\n"
        "/status\n"
        "/jobs"
    )


async def reel_url_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        await reject_unauthorized(update)
        return
    text = (update.effective_message.text or "").strip()
    accounts = account_options()
    overlays = overlay_options()
    try:
        job = reelposter.create_prepare_job(
            text,
            {
                "telegram_chat_id": update.effective_chat.id,
                "telegram_user_id": update.effective_user.id,
                "telegram_position": "br",
                "telegram_size": 16,
                "telegram_destination": "grid",
                "telegram_delay": 0,
                "telegram_scheduled_at": None,
                "telegram_controls_sent": False,
                "telegram_account_id": accounts[0]["id"] if accounts else None,
                "telegram_overlay_id": overlays[0]["id"] if overlays else None,
                "telegram_hide_counts": False,
            },
            rights_confirmed=True,
        )
    except reelposter.ReelPosterError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"Job {job['id'][:8]} added. Downloading the video and metadata..."
    )
    context.application.create_task(
        wait_for_prepare(context.application, job["id"]),
        name=f"prepare-{job['id']}",
    )


async def caption_command(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        await reject_unauthorized(update)
        return
    job = current_job(update.effective_chat.id)
    if not job or job.get("status") != "ready":
        await update.effective_message.reply_text("There is no ready Reel to edit.")
        return
    caption = (update.effective_message.text or "").partition(" ")[2].strip()
    if not caption:
        await update.effective_message.reply_text(
            "Usage: /caption Your replacement caption"
        )
        return
    if len(caption) > 2200:
        await update.effective_message.reply_text(
            "Instagram captions cannot exceed 2,200 characters."
        )
        return
    reelposter.update_job(job["id"], telegram_caption=caption)
    await update.effective_message.reply_text("Caption updated.")


async def schedule_command(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        await reject_unauthorized(update)
        return
    job = current_job(update.effective_chat.id)
    if not job or job.get("status") != "ready":
        await update.effective_message.reply_text("There is no ready Reel to schedule.")
        return
    value = (update.effective_message.text or "").partition(" ")[2].strip()
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
    try:
        local_time = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo(timezone_name)
        )
        scheduled_at = reelposter.parse_schedule_at(local_time.isoformat())
    except (ValueError, reelposter.ReelPosterError) as exc:
        message = (
            str(exc)
            if isinstance(exc, reelposter.ReelPosterError)
            else "Use /schedule YYYY-MM-DD HH:MM"
        )
        await update.effective_message.reply_text(message)
        return
    reelposter.update_job(
        job["id"],
        telegram_scheduled_at=scheduled_at.isoformat(),
        telegram_delay=0,
    )
    await update.effective_message.reply_text(
        f"Scheduled for {value} ({timezone_name})."
    )


async def status_command(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        await reject_unauthorized(update)
        return
    job = current_job(update.effective_chat.id)
    if not job:
        await update.effective_message.reply_text("No ReelPoster jobs yet.")
        return
    await update.effective_message.reply_text(status_text(job))


async def jobs_command(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        await reject_unauthorized(update)
        return
    items = chat_jobs(update.effective_chat.id)[:10]
    if not items:
        await update.effective_message.reply_text("No ReelPoster jobs yet.")
        return
    lines = [
        f"{job['id'][:8]} - {STATUS_LABELS.get(job.get('status'), job.get('status'))}"
        for job in items
    ]
    await update.effective_message.reply_text("Recent jobs:\n" + "\n".join(lines))


def status_text(job: dict) -> str:
    status = STATUS_LABELS.get(job.get("status"), job.get("status", "Unknown"))
    text = f"Job {job['id'][:8]}: {status}\n{job.get('message', '')}".strip()
    if job.get("publish_at"):
        text += f"\nPublish time: {job['publish_at']}"
    if job.get("permalink"):
        text += f"\n{job['permalink']}"
    if job.get("error"):
        text += f"\n{job['error']}"
    if job.get("manual_count_hiding_required"):
        text += (
            "\nAction needed: open this Reel in Instagram and use its settings "
            "to hide like/view counts. Share-count visibility is not exposed "
            "by Meta's publishing API."
        )
    return text


async def control_callback(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not is_authorized(update):
        await reject_unauthorized(update)
        return
    await query.answer()
    job = current_job(update.effective_chat.id)
    if not job or job.get("status") != "ready":
        await query.edit_message_text("This Reel is no longer waiting for options.")
        return

    _, action, value = query.data.split(":", 2)
    if action == "pos" and value in POSITION_PRESETS:
        reelposter.update_job(job["id"], telegram_position=value)
    elif action == "acct" and value == "next":
        reelposter.update_job(
            job["id"],
            telegram_account_id=next_option_id(
                account_options(),
                job.get("telegram_account_id"),
            ),
        )
    elif action == "over" and value == "next":
        reelposter.update_job(
            job["id"],
            telegram_overlay_id=next_option_id(
                overlay_options(),
                job.get("telegram_overlay_id"),
            ),
        )
    elif action == "size" and value in {"10", "16", "22", "28"}:
        reelposter.update_job(job["id"], telegram_size=int(value))
    elif action == "dest" and value in {"grid", "reels-only"}:
        reelposter.update_job(job["id"], telegram_destination=value)
    elif action == "counts" and value == "toggle":
        reelposter.update_job(
            job["id"],
            telegram_hide_counts=not bool(
                job.get("telegram_hide_counts", False)
            ),
        )
    elif action == "delay" and value in {"0", "15", "30", "60"}:
        reelposter.update_job(
            job["id"],
            telegram_delay=int(value),
            telegram_scheduled_at=None,
        )
    elif action == "publish":
        await publish_callback(query, job)
        return

    refreshed = reelposter.get_job_or_404(job["id"])
    try:
        await query.edit_message_text(
            draft_summary(refreshed),
            reply_markup=draft_keyboard(refreshed),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def publish_callback(query, job: dict) -> None:
    position = POSITION_PRESETS[job.get("telegram_position", "br")]
    try:
        queued = reelposter.queue_post_job(
            job_id=job["id"],
            caption=job.get("telegram_caption", job.get("caption", "")),
            size_percent=job.get("telegram_size", 16),
            x_center_percent=position[1],
            y_center_percent=position[2],
            delay_minutes=job.get("telegram_delay", 0),
            scheduled_at=job.get("telegram_scheduled_at"),
            destination=job.get("telegram_destination", "grid"),
            placement_mode="center-v2",
            account_id=job.get("telegram_account_id"),
            overlay_id=job.get("telegram_overlay_id"),
            hide_counts_requested=job.get("telegram_hide_counts", False),
            rights_confirmed=True,
            include_attribution=job.get("source_platform") != "instagram",
        )
    except reelposter.ReelPosterError as exc:
        await query.edit_message_text(f"Could not queue the Reel:\n{exc}")
        return
    reelposter.update_job(
        job["id"],
        telegram_publish_requested=True,
        telegram_last_notified_status=queued["status"],
    )
    await query.edit_message_text(
        "Reel moved to Jobs. Watermarking has started.\n"
        "Send another Reel URL whenever you are ready."
    )


async def notification_loop(application: Application) -> None:
    while True:
        try:
            with reelposter.jobs_lock:
                candidates = [
                    dict(job)
                    for job in reelposter.jobs.values()
                    if job.get("telegram_chat_id")
                ]
            for job in candidates:
                status = job.get("status")
                if status == "ready" and not job.get("telegram_controls_sent"):
                    await send_ready_controls(application, job)
                    continue
                should_notify = job.get("telegram_publish_requested") or status == "error"
                if not should_notify:
                    continue
                if job.get("telegram_last_notified_status") == status:
                    continue
                await application.bot.send_message(
                    chat_id=job["telegram_chat_id"],
                    text=status_text(job),
                )
                reelposter.update_job(
                    job["id"],
                    telegram_last_notified_status=status,
                    telegram_terminal_notified=status in {"done", "error"},
                )
        except TelegramError:
            pass
        except Exception:
            pass
        await asyncio.sleep(5)


async def post_init(application: Application) -> None:
    application.create_task(
        notification_loop(application),
        name="reelposter-notifications",
    )


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .concurrent_updates(False)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("caption", caption_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("jobs", jobs_command))
    application.add_handler(CallbackQueryHandler(control_callback, pattern=r"^rp:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, reel_url_message)
    )
    return application


def run_bot() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = build_application()
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            stop_signals=None,
        )
    finally:
        if not loop.is_closed():
            loop.close()
