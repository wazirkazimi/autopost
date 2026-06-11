from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import cloudinary
import cloudinary.uploader
import requests
import yt_dlp
from dotenv import load_dotenv, set_key
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR_VALUE = os.getenv("REELPOSTER_DATA_DIR", "").strip()
DATA_DIR = Path(DATA_DIR_VALUE or BASE_DIR / "data").resolve()
JOBS_DIR = DATA_DIR / "jobs"
JOBS_STATE_PATH = DATA_DIR / "jobs.json"
ACCOUNTS_STATE_PATH = DATA_DIR / "accounts.json"
OVERLAYS_STATE_PATH = DATA_DIR / "overlays.json"
OVERLAYS_DIR = DATA_DIR / "overlays"
ENV_PATH = BASE_DIR / ".env"
APP_VERSION = "2026.06.11-profiles-v1"
ALLOWED_LOGO_EXTENSIONS = {".png", ".gif"}
ALLOWED_DELAYS = {0, 15, 30, 60}
SETTINGS_KEYS = (
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
    "IG_USER_ID",
    "IG_ACCESS_TOKEN",
)
OPTIONAL_SETTINGS_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
)
STAGES = ("download", "watermark", "upload", "post")
STAGE_LABELS = {
    "download": "Downloading",
    "watermark": "Watermarking",
    "upload": "Uploading",
    "post": "Posting",
}

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
OVERLAYS_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(ENV_PATH, override=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()
profiles_lock = threading.RLock()
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="reelposter")
publish_timers: dict[str, threading.Timer] = {}


@app.before_request
def require_web_auth():
    password = os.getenv("REELPOSTER_WEB_PASSWORD", "").strip()
    if not password or request.path == "/api/health":
        return None

    username = os.getenv("REELPOSTER_WEB_USERNAME", "reelposter").strip()
    auth = request.authorization
    if (
        auth
        and secrets.compare_digest(auth.username or "", username)
        and secrets.compare_digest(auth.password or "", password)
    ):
        return None

    return Response(
        "ReelPoster login required.",
        401,
        {"WWW-Authenticate": 'Basic realm="ReelPoster"'},
    )


@app.after_request
def disable_app_caching(response):
    if response.mimetype in {"text/html", "application/json"}:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


class ReelPosterError(RuntimeError):
    pass


class JobNotFoundError(ReelPosterError):
    pass


class JobNotReadyError(ReelPosterError):
    pass


class OutdatedClientError(ReelPosterError):
    pass


def normalize_access_token(value: str) -> str:
    token = (value or "").strip()
    if token.upper().startswith("IG_ACCESS_TOKEN="):
        token = token.split("=", 1)[1].strip()
    token = token.strip("\"'")
    token = "".join(token.split())
    return token


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def write_json_list(path: Path, payload: list[dict]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def persist_jobs() -> None:
    if app.config.get("TESTING"):
        return

    with jobs_lock:
        payload = list(jobs.values())
        temporary_path = JOBS_STATE_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, JOBS_STATE_PATH)


def load_jobs_state() -> None:
    if app.config.get("TESTING") or not JOBS_STATE_PATH.exists():
        return

    try:
        payload = json.loads(JOBS_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    if not isinstance(payload, list):
        return

    with jobs_lock:
        for saved_job in payload:
            if not isinstance(saved_job, dict) or not saved_job.get("id"):
                continue
            jobs[saved_job["id"]] = saved_job


def add_event(job_id: str, message: str) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["message"] = message
        job["events"].append({"time": utc_now(), "message": message})
        job["updated_at"] = utc_now()
        persist_jobs()


def update_job(job_id: str, **changes) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = utc_now()
        persist_jobs()


def fail_job(job_id: str, exc: Exception) -> None:
    message = str(exc).strip() or exc.__class__.__name__
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["status"] = "error"
        job["error"] = message
        job["message"] = message
        job["events"].append({"time": utc_now(), "message": f"Error: {message}"})
        job["updated_at"] = utc_now()
        persist_jobs()


def public_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "active_stage": job.get("active_stage"),
        "message": job.get("message", ""),
        "error": job.get("error"),
        "caption": job.get("caption", ""),
        "source_url": job.get("source_url", ""),
        "media_id": job.get("media_id"),
        "permalink": job.get("permalink"),
        "delay_minutes": job.get("delay_minutes", 0),
        "publish_at": job.get("publish_at"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "share_to_feed": job.get("share_to_feed", True),
        "account_id": job.get("account_id"),
        "account_name": job.get("account_name"),
        "overlay_id": job.get("overlay_id"),
        "overlay_name": job.get("overlay_name"),
        "logo_placement": {
            "mode": job.get("placement_mode"),
            "size_percent": job.get("logo_size_percent"),
            "x_center_percent": job.get("logo_x_center_percent"),
            "y_center_percent": job.get("logo_y_center_percent"),
        },
        "events": job.get("events", []),
        "stages": build_stages(job),
        "video_ready": bool(job.get("source_path")),
        "thumbnail_url": (
            f"/api/jobs/{job['id']}/thumbnail" if job.get("source_path") else None
        ),
    }


def build_stages(job: dict) -> list[dict]:
    active_stage = job.get("active_stage")
    active_index = STAGES.index(active_stage) if active_stage in STAGES else -1
    result = []

    for index, stage in enumerate(STAGES):
        state = "pending"
        if job["status"] == "done":
            state = "done"
        elif index < active_index:
            state = "done"
        elif index == active_index:
            state = "error" if job["status"] == "error" else "active"
        elif job["status"] == "ready" and stage == "download":
            state = "done"
        result.append({"key": stage, "label": STAGE_LABELS[stage], "state": state})

    return result


def get_job_or_404(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def validate_reel_url(value: str) -> str:
    value = (value or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ReelPosterError("Enter a valid Instagram Reel URL.") from exc

    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    valid_host = host == "instagram.com" or host.endswith(".instagram.com")
    valid_path = "/reel/" in path or "/reels/" in path
    if parsed.scheme not in {"http", "https"} or not valid_host or not valid_path:
        raise ReelPosterError("Enter a public Instagram Reel URL.")
    return value


def locate_downloaded_video(job_dir: Path) -> Path:
    ignored_suffixes = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp"}
    files = [
        path
        for path in job_dir.glob("source.*")
        if path.is_file() and path.suffix.lower() not in ignored_suffixes
    ]
    if not files:
        raise ReelPosterError("The Reel downloaded, but no video file was produced.")
    return max(files, key=lambda path: path.stat().st_size)


def prepare_reel(job_id: str, reel_url: str) -> None:
    try:
        update_job(job_id, status="downloading", active_stage="download")
        add_event(job_id, "Downloading Reel and reading its caption...")
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg = ffmpeg_executable()
        if not ffmpeg:
            raise ReelPosterError(
                "FFmpeg is unavailable. Run pip install -r requirements.txt, "
                "then restart ReelPoster."
            )

        ydl_options = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(job_dir / "source.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "ffmpeg_location": ffmpeg,
        }
        cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
        if cookies_file:
            ydl_options["cookiefile"] = cookies_file

        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(reel_url, download=True)

        source_path = locate_downloaded_video(job_dir)
        caption = (info.get("description") or info.get("title") or "").strip()
        update_job(
            job_id,
            status="ready",
            active_stage=None,
            source_path=str(source_path),
            caption=caption,
        )
        add_event(job_id, "Reel ready. Review the caption and posting options.")
    except Exception as exc:
        fail_job(job_id, normalize_error(exc))


def normalize_error(exc: Exception) -> Exception:
    text = str(exc)
    lower = text.lower()
    if "login required" in lower or "cookies" in lower:
        return ReelPosterError(
            "Instagram blocked the download. Use a public Reel or set "
            "YTDLP_COOKIES_FILE in .env to a valid cookies.txt file."
        )
    return exc


def ffmpeg_executable() -> str | None:
    configured = os.getenv("FFMPEG_BINARY", "").strip()
    if configured and Path(configured).is_file():
        return configured

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        return bundled_ffmpeg if Path(bundled_ffmpeg).is_file() else None
    except (ImportError, RuntimeError):
        return None


def current_logo_path() -> Path | None:
    for extension in ALLOWED_LOGO_EXTENSIONS:
        candidate = DATA_DIR / f"logo{extension}"
        if candidate.exists():
            return candidate
    return None


def environment_account() -> dict | None:
    load_dotenv(ENV_PATH, override=True)
    values = {key: os.getenv(key, "").strip() for key in SETTINGS_KEYS}
    values["IG_ACCESS_TOKEN"] = normalize_access_token(values["IG_ACCESS_TOKEN"])
    if not all(values.values()):
        return None
    return {
        "id": "environment",
        "name": os.getenv(
            "REELPOSTER_DEFAULT_ACCOUNT_NAME",
            "Environment account",
        ).strip()
        or "Environment account",
        **values,
    }


def account_profiles() -> list[dict]:
    with profiles_lock:
        profiles = read_json_list(ACCOUNTS_STATE_PATH)
    environment = environment_account()
    return ([environment] if environment else []) + profiles


def public_account(profile: dict) -> dict:
    return {
        "id": profile["id"],
        "name": profile.get("name") or "Instagram account",
        "ig_user_id": profile.get("IG_USER_ID", ""),
        "source": "environment" if profile["id"] == "environment" else "saved",
    }


def resolve_account_settings(account_id: str | None = None) -> dict[str, str]:
    profiles = account_profiles()
    if not profiles:
        raise ReelPosterError("Add an Instagram publishing account in Setup.")
    profile = next(
        (item for item in profiles if item["id"] == account_id),
        profiles[0] if not account_id else None,
    )
    if not profile:
        raise ReelPosterError("The selected Instagram account no longer exists.")
    values = {key: str(profile.get(key, "")).strip() for key in SETTINGS_KEYS}
    values["IG_ACCESS_TOKEN"] = normalize_access_token(values["IG_ACCESS_TOKEN"])
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ReelPosterError(
            "The selected account is incomplete. Missing: " + ", ".join(missing)
        )
    if not values["IG_ACCESS_TOKEN"].startswith(("IGAA", "IGQ", "EAA")):
        raise ReelPosterError(
            "The selected account has an invalid Instagram access token."
        )
    return {
        **values,
        "ACCOUNT_ID": profile["id"],
        "ACCOUNT_NAME": profile.get("name") or "Instagram account",
    }


def save_account_profile(data) -> dict:
    name = str(data.get("name", "")).strip()
    if not name:
        raise ReelPosterError("Enter an account name.")
    values = {key: str(data.get(key, "")).strip() for key in SETTINGS_KEYS}
    values["IG_ACCESS_TOKEN"] = normalize_access_token(values["IG_ACCESS_TOKEN"])
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ReelPosterError(
            "Complete all account credentials. Missing: " + ", ".join(missing)
        )
    if not values["IG_ACCESS_TOKEN"].startswith(("IGAA", "IGQ", "EAA")):
        raise ReelPosterError("The Instagram access token format is invalid.")
    now = utc_now()
    profile = {
        "id": uuid.uuid4().hex[:12],
        "name": name[:80],
        **values,
        "created_at": now,
        "updated_at": now,
    }
    with profiles_lock:
        profiles = read_json_list(ACCOUNTS_STATE_PATH)
        profiles.append(profile)
        write_json_list(ACCOUNTS_STATE_PATH, profiles)
    return profile


def update_account_profile_user_id(account_id: str, user_id: str) -> None:
    with profiles_lock:
        profiles = read_json_list(ACCOUNTS_STATE_PATH)
        for profile in profiles:
            if profile.get("id") == account_id:
                profile["IG_USER_ID"] = user_id
                profile["updated_at"] = utc_now()
                write_json_list(ACCOUNTS_STATE_PATH, profiles)
                return


def overlay_profiles() -> list[dict]:
    with profiles_lock:
        saved = read_json_list(OVERLAYS_STATE_PATH)
    result = []
    legacy_path = current_logo_path()
    if legacy_path:
        result.append(
            {
                "id": "default",
                "name": "Default overlay",
                "file_name": legacy_path.name,
                "legacy": True,
            }
        )
    for profile in saved:
        path = OVERLAYS_DIR / str(profile.get("file_name", ""))
        if path.is_file():
            result.append(profile)
    return result


def public_overlay(profile: dict) -> dict:
    overlay_id = profile["id"]
    return {
        "id": overlay_id,
        "name": profile.get("name") or "Overlay",
        "url": (
            "/api/logo"
            if overlay_id == "default"
            else f"/api/overlays/{overlay_id}/file"
        ),
        "animated": str(profile.get("file_name", "")).lower().endswith(".gif"),
    }


def resolve_overlay_path(overlay_id: str | None = None) -> tuple[Path, dict]:
    profiles = overlay_profiles()
    if not profiles:
        raise ReelPosterError("Upload a PNG or GIF overlay in Setup.")
    profile = next(
        (item for item in profiles if item["id"] == overlay_id),
        profiles[0] if not overlay_id else None,
    )
    if not profile:
        raise ReelPosterError("The selected overlay no longer exists.")
    path = (
        current_logo_path()
        if profile["id"] == "default"
        else OVERLAYS_DIR / profile["file_name"]
    )
    if not path or not path.is_file():
        raise ReelPosterError("The selected overlay file is unavailable.")
    return path, profile


def save_overlay_profile(upload, name: str) -> dict:
    filename = secure_filename(upload.filename or "")
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_LOGO_EXTENSIONS:
        raise ReelPosterError("Overlay must be a PNG or GIF file.")
    overlay_id = uuid.uuid4().hex[:12]
    file_name = f"{overlay_id}{extension}"
    upload.save(OVERLAYS_DIR / file_name)
    now = utc_now()
    profile = {
        "id": overlay_id,
        "name": (name.strip() or Path(filename).stem or "Overlay")[:80],
        "file_name": file_name,
        "created_at": now,
        "updated_at": now,
    }
    with profiles_lock:
        profiles = read_json_list(OVERLAYS_STATE_PATH)
        profiles.append(profile)
        write_json_list(OVERLAYS_STATE_PATH, profiles)
    return profile


def require_settings() -> dict[str, str]:
    return resolve_account_settings()


def probe_video_dimensions(path: Path) -> tuple[int, int]:
    ffmpeg = ffmpeg_executable()
    if not ffmpeg:
        raise ReelPosterError("FFmpeg is unavailable.")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    matches = re.findall(
        r"Video:.*?,\s*(\d{2,5})x(\d{2,5})(?:[\s,\[])",
        result.stderr,
    )
    if not matches:
        raise ReelPosterError("Could not determine the Reel's video dimensions.")
    width, height = matches[0]
    return int(width), int(height)


def watermark_video(
    source_path: Path,
    logo_path: Path,
    output_path: Path,
    size_percent: int,
    x_center_percent: float,
    y_center_percent: float,
) -> None:
    ffmpeg = ffmpeg_executable()
    if not ffmpeg:
        raise ReelPosterError(
            "FFmpeg is unavailable. Reinstall the project dependencies with "
            "pip install -r requirements.txt."
        )

    video_width, _video_height = probe_video_dimensions(source_path)
    logo_width = max(16, round(video_width * size_percent / 100))
    x_ratio = x_center_percent / 100
    y_ratio = y_center_percent / 100
    x = (
        "max(0\\,min(main_w-overlay_w\\,"
        f"main_w*{x_ratio:.6f}-overlay_w/2))"
    )
    y = (
        "max(0\\,min(main_h-overlay_h\\,"
        f"main_h*{y_ratio:.6f}-overlay_h/2))"
    )

    command = [ffmpeg, "-y", "-i", str(source_path)]
    if logo_path.suffix.lower() == ".gif":
        command.extend(["-stream_loop", "-1", "-i", str(logo_path)])
    else:
        command.extend(["-loop", "1", "-i", str(logo_path)])

    filter_graph = (
        f"[1:v]scale={logo_width}:-1:flags=lanczos,"
        "format=rgba[logo];"
        f"[0:v][logo]overlay={x}:{y}:format=auto:shortest=1[v]"
    )
    command.extend(
        [
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output_path),
        ]
    )

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        tail = "\n".join(detail[-8:])
        raise ReelPosterError(f"FFmpeg could not add the logo.\n{tail}")


def upload_to_cloudinary(video_path: Path, settings: dict[str, str], job_id: str) -> str:
    cloudinary.config(
        cloud_name=settings["CLOUDINARY_CLOUD_NAME"],
        api_key=settings["CLOUDINARY_API_KEY"],
        api_secret=settings["CLOUDINARY_API_SECRET"],
        secure=True,
    )
    upload_method = (
        cloudinary.uploader.upload_large
        if video_path.stat().st_size > 95 * 1024 * 1024
        else cloudinary.uploader.upload
    )
    response = upload_method(
        str(video_path),
        resource_type="video",
        folder="reelposter",
        public_id=job_id,
        overwrite=True,
    )
    secure_url = response.get("secure_url")
    if not secure_url:
        raise ReelPosterError("Cloudinary did not return a public video URL.")
    return secure_url


def graph_api_base(access_token: str) -> str:
    configured = os.getenv("IG_GRAPH_API_HOST", "").strip().rstrip("/")
    if configured:
        return configured
    if access_token.startswith(("IGAA", "IGQ")):
        return "https://graph.instagram.com"
    return "https://graph.facebook.com"


def graph_post(path: str, data: dict) -> dict:
    version = os.getenv("IG_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"
    access_token = str(data.get("access_token", ""))
    response = requests.post(
        f"{graph_api_base(access_token)}/{version}/{path.lstrip('/')}",
        data=data,
        timeout=60,
    )
    return parse_graph_response(response)


def graph_get(path: str, params: dict) -> dict:
    version = os.getenv("IG_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"
    access_token = str(params.get("access_token", ""))
    response = requests.get(
        f"{graph_api_base(access_token)}/{version}/{path.lstrip('/')}",
        params=params,
        timeout=30,
    )
    return parse_graph_response(response)


def parse_graph_response(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReelPosterError(
            f"Instagram returned an unreadable response ({response.status_code})."
        ) from exc

    if not response.ok or "error" in payload:
        error = payload.get("error", {})
        message = error.get("message") or f"Instagram API error {response.status_code}"
        detail = error.get("error_user_msg")
        if "cannot parse access token" in message.lower():
            raise ReelPosterError(
                "Instagram could not parse the access token. Generate a new token "
                "and paste only its value in Setup, without IG_ACCESS_TOKEN=, "
                "quotes, or spaces."
            )
        raise ReelPosterError(f"{message}{': ' + detail if detail else ''}")
    return payload


def verify_instagram_credentials(settings: dict[str, str]) -> dict:
    access_token = settings["IG_ACCESS_TOKEN"]
    instagram_login = graph_api_base(access_token) == "https://graph.instagram.com"
    path = "me" if instagram_login else settings["IG_USER_ID"]
    fields = "user_id,username" if instagram_login else "id,username"
    result = graph_get(
        path,
        {"fields": fields, "access_token": access_token},
    )
    returned_id = str(result.get("user_id") or result.get("id") or "")
    if returned_id and returned_id != settings["IG_USER_ID"]:
        if instagram_login:
            settings["IG_USER_ID"] = returned_id
            account_id = settings.get("ACCOUNT_ID", "environment")
            if account_id == "environment":
                ENV_PATH.touch(exist_ok=True)
                set_key(str(ENV_PATH), "IG_USER_ID", returned_id, quote_mode="always")
                os.environ["IG_USER_ID"] = returned_id
            else:
                update_account_profile_user_id(account_id, returned_id)
        else:
            raise ReelPosterError(
                "IG_USER_ID does not match this access token. Use the Instagram "
                "Business Account ID connected to the selected Facebook Page."
            )
    return result


def wait_for_container(container_id: str, access_token: str, timeout_seconds: int = 600) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = graph_get(
            container_id,
            {"fields": "status_code,status", "access_token": access_token},
        )
        status = (result.get("status_code") or "").upper()
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            detail = result.get("status") or status
            raise ReelPosterError(
                "Instagram could not process the Reel "
                f"(container {container_id}, status {detail})."
            )
        time.sleep(5)
    raise ReelPosterError("Instagram took too long to process the Reel container.")


def validate_public_video_url(video_url: str) -> None:
    try:
        response = requests.get(
            video_url,
            headers={"Range": "bytes=0-1023", "User-Agent": "ReelPoster/1.0"},
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise ReelPosterError(
            "Cloudinary uploaded the video, but its public URL could not be reached."
        ) from exc
    content_type = response.headers.get("content-type", "").lower()
    if response.status_code not in {200, 206} or "video/" not in content_type:
        raise ReelPosterError(
            "Cloudinary's public URL is not returning a directly downloadable video "
            f"(HTTP {response.status_code}, {content_type or 'unknown content type'})."
        )


def create_and_process_container(
    job_id: str,
    job: dict,
    settings: dict[str, str],
) -> str:
    last_error = None
    for attempt in range(1, 3):
        if attempt > 1:
            add_event(
                job_id,
                "Instagram processing failed once. Retrying with a new container...",
            )
            time.sleep(10)

        container = graph_post(
            f"{settings['IG_USER_ID']}/media",
            {
                "media_type": "REELS",
                "video_url": job["cloudinary_url"],
                "caption": job.get("caption", ""),
                "share_to_feed": (
                    "true" if job.get("share_to_feed", True) else "false"
                ),
                "access_token": settings["IG_ACCESS_TOKEN"],
            },
        )
        container_id = container.get("id")
        if not container_id:
            raise ReelPosterError("Instagram did not return a Reel container ID.")
        update_job(job_id, container_id=container_id)
        add_event(job_id, "Instagram is processing the uploaded video...")
        try:
            wait_for_container(container_id, settings["IG_ACCESS_TOKEN"])
            return container_id
        except ReelPosterError as exc:
            last_error = exc
            if attempt == 2:
                break

    raise ReelPosterError(
        f"{last_error} The video passed local and public-URL checks. "
        "Wait a few minutes and try again; Meta may be temporarily rejecting "
        "new media containers."
    )


def parse_schedule_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReelPosterError("Choose a valid posting date and time.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed <= now:
        raise ReelPosterError("Scheduled posting time must be in the future.")
    if (parsed - now).days > 90:
        raise ReelPosterError("Scheduled posting time must be within 90 days.")
    return parsed


def schedule_instagram_publish(
    job_id: str,
    scheduled_at: datetime | None,
    delay_minutes: int,
) -> bool:
    publish_at = scheduled_at
    if publish_at is None and delay_minutes:
        publish_at = datetime.fromtimestamp(
            time.time() + (delay_minutes * 60),
            tz=timezone.utc,
        )
    if publish_at is None:
        return False

    seconds = max(0, (publish_at - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 1:
        return False

    update_job(
        job_id,
        status="scheduled",
        active_stage="post",
        publish_at=publish_at.isoformat(),
    )
    add_event(
        job_id,
        f"Scheduled for {publish_at.strftime('%Y-%m-%d %H:%M UTC')}. "
        "Keep ReelPoster running.",
    )
    timer = threading.Timer(
        seconds,
        lambda: executor.submit(publish_uploaded_reel, job_id),
    )
    timer.daemon = True
    with jobs_lock:
        publish_timers[job_id] = timer
    timer.start()
    return True


def fetch_permalink(media_id: str, access_token: str) -> str | None:
    try:
        result = graph_get(
            media_id,
            {"fields": "permalink", "access_token": access_token},
        )
        return result.get("permalink")
    except Exception:
        return None


def process_reel(
    job_id: str,
    caption: str,
    size_percent: int,
    x_center_percent: float,
    y_center_percent: float,
    delay_minutes: int,
    scheduled_at: datetime | None,
    share_to_feed: bool,
    account_id: str | None = None,
    overlay_id: str | None = None,
) -> None:
    try:
        settings = resolve_account_settings(account_id)
        add_event(job_id, "Checking Instagram credentials...")
        verify_instagram_credentials(settings)
        logo_path, overlay = resolve_overlay_path(overlay_id)

        job = get_job_or_404(job_id)
        if not job or not job.get("source_path"):
            raise ReelPosterError("The downloaded Reel is no longer available.")
        source_path = Path(job["source_path"])
        output_path = source_path.parent / "watermarked.mp4"

        update_job(job_id, status="watermarking", active_stage="watermark")
        add_event(job_id, "Adding your logo with FFmpeg...")
        watermark_video(
            source_path,
            logo_path,
            output_path,
            size_percent,
            x_center_percent,
            y_center_percent,
        )

        update_job(job_id, status="uploading", active_stage="upload")
        add_event(job_id, "Uploading the finished video to Cloudinary...")
        video_url = upload_to_cloudinary(output_path, settings, job_id)
        validate_public_video_url(video_url)
        update_job(
            job_id,
            cloudinary_url=video_url,
            caption=caption,
            share_to_feed=share_to_feed,
            logo_size_percent=size_percent,
            logo_x_center_percent=x_center_percent,
            logo_y_center_percent=y_center_percent,
            account_id=settings["ACCOUNT_ID"],
            account_name=settings["ACCOUNT_NAME"],
            overlay_id=overlay["id"],
            overlay_name=overlay.get("name") or "Overlay",
        )

        if schedule_instagram_publish(job_id, scheduled_at, delay_minutes):
            return
        publish_uploaded_reel(job_id)
    except Exception as exc:
        fail_job(job_id, normalize_error(exc))


def publish_uploaded_reel(job_id: str) -> None:
    try:
        job = get_job_or_404(job_id)
        if not job or not job.get("cloudinary_url"):
            raise ReelPosterError("The uploaded Reel is no longer available.")
        settings = resolve_account_settings(job.get("account_id"))
        verify_instagram_credentials(settings)

        update_job(
            job_id,
            status="posting",
            active_stage="post",
            publish_at=None,
        )
        add_event(job_id, "Creating the Instagram Reel container...")
        container_id = create_and_process_container(job_id, job, settings)

        add_event(job_id, "Publishing the Reel to Instagram...")
        published = graph_post(
            f"{settings['IG_USER_ID']}/media_publish",
            {
                "creation_id": container_id,
                "access_token": settings["IG_ACCESS_TOKEN"],
            },
        )
        media_id = published.get("id")
        permalink = (
            fetch_permalink(media_id, settings["IG_ACCESS_TOKEN"]) if media_id else None
        )
        update_job(
            job_id,
            status="done",
            active_stage=None,
            media_id=media_id,
            permalink=permalink,
            publish_at=None,
        )
        with jobs_lock:
            publish_timers.pop(job_id, None)
        add_event(job_id, "Done. Your Reel is live on Instagram.")
    except Exception as exc:
        fail_job(job_id, normalize_error(exc))


def create_prepare_job(reel_url: str, metadata: dict | None = None) -> dict:
    reel_url = validate_reel_url(reel_url)
    job_id = uuid.uuid4().hex
    now = utc_now()
    job = {
        "id": job_id,
        "source_url": reel_url,
        "status": "queued",
        "active_stage": "download",
        "message": "Queued",
        "error": None,
        "caption": "",
        "events": [{"time": now, "message": "Reel added to the queue."}],
        "created_at": now,
        "updated_at": now,
    }
    if metadata:
        job.update(
            {
                key: value
                for key, value in metadata.items()
                if key.startswith("telegram_")
            }
        )

    with jobs_lock:
        jobs[job_id] = job
        persist_jobs()
    executor.submit(prepare_reel, job_id, reel_url)
    return public_job(job)


def queue_post_job(
    job_id: str,
    caption: str,
    size_percent: int,
    x_center_percent: float,
    y_center_percent: float,
    delay_minutes: int = 0,
    scheduled_at: str | datetime | None = None,
    destination: str = "grid",
    placement_mode: str = "center-v2",
    account_id: str | None = None,
    overlay_id: str | None = None,
) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise JobNotFoundError("Job not found.")
        if job["status"] != "ready":
            raise JobNotReadyError("This Reel is not ready to post.")

    caption = str(caption or "").strip()
    if len(caption) > 2200:
        raise ReelPosterError(
            "Instagram captions cannot exceed 2,200 characters."
        )
    if placement_mode != "center-v2":
        raise OutdatedClientError(
            "The ReelPoster page and server are out of date. "
            "Refresh the page and try again."
        )

    try:
        size_percent = int(size_percent)
        x_center_percent = float(x_center_percent)
        y_center_percent = float(y_center_percent)
        delay_minutes = int(delay_minutes)
    except (TypeError, ValueError) as exc:
        raise ReelPosterError(
            "Logo placement and posting delay must be numbers."
        ) from exc
    if not 5 <= size_percent <= 35:
        raise ReelPosterError("Logo size must be between 5% and 35%.")
    if not 0 <= x_center_percent <= 100 or not 0 <= y_center_percent <= 100:
        raise ReelPosterError("Logo placement must stay inside the video.")
    if delay_minutes not in ALLOWED_DELAYS:
        raise ReelPosterError("Choose a supported posting delay.")

    if isinstance(scheduled_at, datetime):
        scheduled_at = parse_schedule_at(scheduled_at.isoformat())
    else:
        scheduled_at = parse_schedule_at(scheduled_at)
    if destination not in {"grid", "reels-only"}:
        raise ReelPosterError("Choose a valid Instagram destination.")
    share_to_feed = destination == "grid"
    account = resolve_account_settings(account_id)
    _, overlay = resolve_overlay_path(overlay_id)

    update_job(
        job_id,
        caption=caption,
        delay_minutes=delay_minutes,
        share_to_feed=share_to_feed,
        placement_mode="center-v2",
        logo_size_percent=size_percent,
        logo_x_center_percent=x_center_percent,
        logo_y_center_percent=y_center_percent,
        account_id=account["ACCOUNT_ID"],
        account_name=account["ACCOUNT_NAME"],
        overlay_id=overlay["id"],
        overlay_name=overlay.get("name") or "Overlay",
        status="watermarking",
        active_stage="watermark",
        error=None,
    )
    executor.submit(
        process_reel,
        job_id,
        caption,
        size_percent,
        x_center_percent,
        y_center_percent,
        delay_minutes,
        scheduled_at,
        share_to_feed,
        account["ACCOUNT_ID"],
        overlay["id"],
    )
    return public_job(get_job_or_404(job_id))


def restore_jobs_state() -> None:
    load_jobs_state()
    with jobs_lock:
        saved_jobs = [dict(job) for job in jobs.values()]

    interrupted_statuses = {
        "queued",
        "downloading",
        "watermarking",
        "uploading",
        "posting",
    }
    for job in saved_jobs:
        job_id = job["id"]
        status = job.get("status")
        if status in interrupted_statuses:
            fail_job(
                job_id,
                ReelPosterError(
                    "This job was interrupted by a server restart. "
                    "Fetch the Reel again."
                ),
            )
            continue
        if status == "ready":
            source_path = Path(job.get("source_path", ""))
            if not source_path.is_file():
                fail_job(
                    job_id,
                    ReelPosterError(
                        "The downloaded Reel is no longer available. "
                        "Fetch it again."
                    ),
                )
            continue
        if status != "scheduled":
            continue
        if not job.get("cloudinary_url") or not job.get("publish_at"):
            fail_job(
                job_id,
                ReelPosterError(
                    "The scheduled Reel could not be restored after restart."
                ),
            )
            continue
        try:
            publish_at = datetime.fromisoformat(
                str(job["publish_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError:
            fail_job(job_id, ReelPosterError("The saved schedule is invalid."))
            continue

        seconds = (publish_at - datetime.now(timezone.utc)).total_seconds()
        if seconds <= 1:
            executor.submit(publish_uploaded_reel, job_id)
            continue
        timer = threading.Timer(
            seconds,
            lambda saved_job_id=job_id: executor.submit(
                publish_uploaded_reel,
                saved_job_id,
            ),
        )
        timer.daemon = True
        with jobs_lock:
            publish_timers[job_id] = timer
        timer.start()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    ffmpeg = ffmpeg_executable()
    telegram_thread = app.config.get("TELEGRAM_BOT_THREAD")
    allowed_telegram_users = [
        value.strip()
        for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
        if value.strip().isdigit()
    ]
    return jsonify(
        {
            "ok": True,
            "app_version": APP_VERSION,
            "placement_mode": "center-v2",
            "job_queue": True,
            "job_overview": True,
            "ffmpeg": bool(ffmpeg),
            "configured": bool(account_profiles()),
            "logo": bool(overlay_profiles()),
            "account_profiles": len(account_profiles()),
            "overlay_profiles": len(overlay_profiles()),
            "telegram": {
                "configured": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
                "allowed_users": len(allowed_telegram_users),
                "running": bool(
                    telegram_thread
                    and telegram_thread.is_alive()
                ),
            },
        }
    )


@app.get("/api/settings")
def get_settings():
    load_dotenv(ENV_PATH, override=True)
    return jsonify(
        {
            "configured": {
                key: bool(os.getenv(key, "").strip()) for key in SETTINGS_KEYS
            },
            "telegram_configured": {
                key: bool(os.getenv(key, "").strip())
                for key in OPTIONAL_SETTINGS_KEYS
            },
            "app_timezone": os.getenv("APP_TIMEZONE", "Asia/Kolkata"),
            "graph_api_version": os.getenv("IG_GRAPH_API_VERSION", "v25.0"),
            "logo_url": "/api/logo" if current_logo_path() else None,
            "accounts": [public_account(item) for item in account_profiles()],
            "overlays": [public_overlay(item) for item in overlay_profiles()],
        }
    )


@app.get("/api/accounts")
def list_accounts():
    return jsonify(
        {"accounts": [public_account(item) for item in account_profiles()]}
    )


@app.post("/api/accounts")
def create_account():
    try:
        profile = save_account_profile(request.form)
    except ReelPosterError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "account": public_account(profile)}), 201


@app.get("/api/overlays")
def list_overlays():
    return jsonify(
        {"overlays": [public_overlay(item) for item in overlay_profiles()]}
    )


@app.post("/api/overlays")
def create_overlay():
    upload = request.files.get("overlay")
    if not upload or not upload.filename:
        return jsonify({"error": "Choose a PNG or GIF overlay."}), 400
    try:
        profile = save_overlay_profile(
            upload,
            request.form.get("name", ""),
        )
    except ReelPosterError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "overlay": public_overlay(profile)}), 201


@app.get("/api/overlays/<overlay_id>/file")
def get_overlay_file(overlay_id: str):
    try:
        path, _ = resolve_overlay_path(overlay_id)
    except ReelPosterError as exc:
        return jsonify({"error": str(exc)}), 404
    return send_file(path)


@app.post("/api/settings")
def save_settings():
    data = request.form
    ENV_PATH.touch(exist_ok=True)

    for key in SETTINGS_KEYS + OPTIONAL_SETTINGS_KEYS:
        value = data.get(key, "").strip()
        if key == "IG_ACCESS_TOKEN":
            value = normalize_access_token(value)
            if value and not value.startswith(("IGAA", "IGQ", "EAA")):
                return jsonify(
                    {
                        "error": (
                            "Access token format is invalid. Paste only the token "
                            "value, without IG_ACCESS_TOKEN= or quotation marks."
                        )
                    }
                ), 400
        if key == "TELEGRAM_ALLOWED_USER_IDS" and value:
            user_ids = [item.strip() for item in value.split(",")]
            if not all(item.isdigit() for item in user_ids):
                return jsonify(
                    {
                        "error": (
                            "Telegram allowed user IDs must be numeric and "
                            "comma-separated."
                        )
                    }
                ), 400
        if value:
            set_key(str(ENV_PATH), key, value, quote_mode="always")

    graph_version = data.get("IG_GRAPH_API_VERSION", "").strip()
    if graph_version:
        if not graph_version.startswith("v") or not graph_version[1:].replace(".", "").isdigit():
            return jsonify({"error": "Graph API version must look like v25.0."}), 400
        set_key(str(ENV_PATH), "IG_GRAPH_API_VERSION", graph_version, quote_mode="always")

    app_timezone = data.get("APP_TIMEZONE", "").strip()
    if app_timezone:
        try:
            ZoneInfo(app_timezone)
        except ZoneInfoNotFoundError:
            return jsonify(
                {"error": "Use a valid IANA timezone such as Asia/Kolkata."}
            ), 400
        set_key(str(ENV_PATH), "APP_TIMEZONE", app_timezone, quote_mode="always")

    logo = request.files.get("logo")
    if logo and logo.filename:
        filename = secure_filename(logo.filename)
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_LOGO_EXTENSIONS:
            return jsonify({"error": "Logo must be a PNG or GIF file."}), 400
        for old_logo in DATA_DIR.glob("logo.*"):
            if old_logo.suffix.lower() in ALLOWED_LOGO_EXTENSIONS:
                old_logo.unlink(missing_ok=True)
        logo.save(DATA_DIR / f"logo{extension}")

    telegram_changed = any(
        data.get(key, "").strip() for key in OPTIONAL_SETTINGS_KEYS
    )
    load_dotenv(ENV_PATH, override=True)
    message = "Setup saved locally."
    if telegram_changed:
        message += " Restart ReelPoster to apply Telegram bot changes."
    return jsonify({"ok": True, "message": message})


@app.post("/api/settings/test")
def test_settings():
    try:
        settings = require_settings()
        for key in ("IG_USER_ID", "IG_ACCESS_TOKEN"):
            submitted = request.form.get(key, "").strip()
            if submitted:
                settings[key] = (
                    normalize_access_token(submitted)
                    if key == "IG_ACCESS_TOKEN"
                    else submitted
                )
        profile = verify_instagram_credentials(settings)
    except ReelPosterError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "username": profile.get("username"),
            "message": "Instagram credentials are valid.",
        }
    )


@app.get("/api/logo")
def get_logo():
    logo_path = current_logo_path()
    if not logo_path:
        return jsonify({"error": "No logo has been uploaded."}), 404
    return send_file(logo_path)


@app.post("/api/reels/prepare")
def start_prepare():
    payload = request.get_json(silent=True) or {}
    try:
        job = create_prepare_job(payload.get("url", ""))
    except ReelPosterError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


@app.post("/api/reels/<job_id>/post")
def start_post(job_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        job = queue_post_job(
            job_id=job_id,
            caption=payload.get("caption", ""),
            size_percent=payload.get("size_percent", 16),
            x_center_percent=payload.get("x_center_percent"),
            y_center_percent=payload.get("y_center_percent"),
            delay_minutes=payload.get("delay_minutes", 0),
            scheduled_at=payload.get("scheduled_at"),
            destination=payload.get("destination", "grid"),
            placement_mode=payload.get("placement_mode"),
            account_id=payload.get("account_id"),
            overlay_id=payload.get("overlay_id"),
        )
    except JobNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except (JobNotReadyError, OutdatedClientError) as exc:
        return jsonify({"error": str(exc)}), 409
    except ReelPosterError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = get_job_or_404(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(public_job(job))


@app.get("/api/jobs")
def list_jobs():
    with jobs_lock:
        ordered = sorted(
            (dict(job) for job in jobs.values()),
            key=lambda job: job.get("created_at", ""),
            reverse=True,
        )
    return jsonify({"jobs": [public_job(job) for job in ordered]})


@app.get("/api/jobs/<job_id>/video")
def job_video(job_id: str):
    job = get_job_or_404(job_id)
    if not job or not job.get("source_path"):
        return jsonify({"error": "Video is not ready."}), 404
    path = Path(job["source_path"])
    if not path.exists():
        return jsonify({"error": "Video file is no longer available."}), 404
    return send_file(path, conditional=True)


@app.get("/api/jobs/<job_id>/thumbnail")
def job_thumbnail(job_id: str):
    job = get_job_or_404(job_id)
    if not job or not job.get("source_path"):
        return jsonify({"error": "Preview is not ready."}), 404
    source_path = Path(job["source_path"])
    if not source_path.exists():
        return jsonify({"error": "Video file is no longer available."}), 404

    thumbnail_path = source_path.parent / "preview-v2.jpg"
    if not thumbnail_path.exists():
        ffmpeg = ffmpeg_executable()
        if not ffmpeg:
            return jsonify({"error": "FFmpeg is unavailable."}), 503
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-ss",
                "0.5",
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-vf",
                (
                    "scale=360:640:force_original_aspect_ratio=decrease:"
                    "flags=lanczos,"
                    "pad=360:640:(ow-iw)/2:(oh-ih)/2:black"
                ),
                "-q:v",
                "3",
                str(thumbnail_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not thumbnail_path.exists():
            return jsonify({"error": "Could not create the Reel preview."}), 500
    return send_file(thumbnail_path, conditional=True, max_age=3600)


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": "Logo is too large. Keep it under 10 MB."}), 413


if __name__ == "__main__":
    restore_jobs_state()
    debug = os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=False, threaded=True)
