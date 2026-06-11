# ReelPoster

ReelPoster is a Flask app and private Telegram bot for downloading an Instagram
Reel, adding your logo with FFmpeg, uploading the finished video to Cloudinary,
and publishing it through the Instagram Graph API.

## Requirements

- Python 3.10 or newer
- FFmpeg on `PATH`, or the bundled FFmpeg installed by `requirements.txt`
- A Cloudinary account
- An Instagram Business or Creator account connected for Graph API publishing
- A long-lived access token with `instagram_content_publish`
- A Telegram bot token from `@BotFather` (optional)

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py render_app.py
```

Open `http://127.0.0.1:5000`, choose **Setup**, upload a PNG or GIF logo, and
enter:

- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `IG_USER_ID`
- `IG_ACCESS_TOKEN`

The setup form writes these values to the local `.env` file. The file and
generated media under `data/` are excluded from Git.

`imageio-ffmpeg` supplies a project-local FFmpeg binary by default, so a
separate system installation is normally unnecessary. You can set
`FFMPEG_BINARY` in `.env` to use a specific `ffmpeg.exe`.

## Telegram bot

1. Open `@BotFather` in Telegram, run `/newbot`, and copy the token.
2. Set `TELEGRAM_BOT_TOKEN` in `.env`.
3. Start the app and send `/start` to the bot. It replies with your numeric
   Telegram user ID.
4. Put that ID in `TELEGRAM_ALLOWED_USER_IDS` and restart the app.
5. Send a public Instagram Reel URL to the bot.

The bot fetches the caption, offers logo corner and size presets, lets you pick
Grid + Reels or Reels-only, and supports a 0/15/30/60 minute delay. Use
`/schedule YYYY-MM-DD HH:MM` for an exact time in `APP_TIMEZONE`, and
`/caption NEW TEXT` to replace the fetched caption.

The web UI remains the best option for exact drag positioning. Telegram uses
four reliable corner presets.

## Multiple accounts and overlays

The credentials configured through environment variables appear as the
**Environment account**. Add more publishing profiles from Setup; each profile
can have its own Instagram Business Account ID, access token, and Cloudinary
credentials.

Upload multiple PNG or GIF overlays from Setup. Choose the publishing account
and overlay for each Reel from the web dropdowns. In Telegram, tap the Account
or Overlay button to cycle through the saved choices before publishing.

The **Hide like and view counts after publishing** preference is off by default.
Meta's documented Reel publishing API does not provide a parameter for hiding
like/view counts or share counts. When enabled, ReelPoster preserves the
preference and sends a post-publish reminder with the Reel link so you can apply
the setting in Instagram without risking a failed media container.

Saved account profiles, overlay metadata, and overlay files live under `data/`.
On Render, that directory is backed by the persistent disk configured in
`render.yaml`. API responses never return saved access tokens or Cloudinary
secrets.

## Publishing flow

1. Paste a public Instagram Reel URL and fetch it.
2. Review or edit the extracted caption.
3. Pick the logo position, size, and optional posting delay.
4. ReelPoster watermarks the video, uploads it, creates an Instagram Reel
   container, waits for processing, and publishes it.

The logo keeps its original PNG/GIF aspect ratio. After you click
**Watermark & Post**, the task moves to **Jobs** and the Create screen is
immediately available for another Reel.

The optional delay or calendar schedule starts after the Cloudinary upload and
before ReelPoster creates the Instagram container. Keep ReelPoster running until
scheduled jobs finish.

Job state is stored in `data/jobs.json`. On startup, pending scheduled posts are
restored from disk. A job that was actively downloading, watermarking,
uploading, or posting during a restart is marked interrupted instead of being
published twice.

## Deploy to Render

Render is enough for a personal, low-volume ReelPoster deployment, but use the
paid Starter web service defined in `render.yaml`. The free web service is not
appropriate because it can restart or spin down and does not support the
persistent disk needed for logos, job state, and downloaded media.

1. Push this repository to a private GitHub repository.
2. In Render, choose **New > Blueprint** and select the repository.
3. Render reads `render.yaml` and asks for the secret environment values.
4. Enter the Cloudinary, Instagram, and Telegram values from `.env.example`.
   Set `REELPOSTER_WEB_PASSWORD` to a long, unique password. The username
   defaults to `reelposter`.
5. Deploy, open the Render URL once, and upload the logo on the Setup page.

The deployment deliberately uses one Gunicorn worker. Telegram long polling
runs inside that process; additional workers would start duplicate bot
instances. The 10 GB persistent disk is mounted at
`/opt/render/project/src/data`.

If Instagram blocks yt-dlp from Render's IP address, add a Render Secret File
containing Netscape cookies and set `YTDLP_COOKIES_FILE` to that file's path.

## Instagram download notes

Instagram sometimes requires a logged-in session even for content that appears
public. Export a Netscape-format `cookies.txt` file and set
`YTDLP_COOKIES_FILE` in `.env` when yt-dlp reports a login or cookies error.

Only repost content you own or have permission to use. Download behavior and
publishing access can change when Instagram updates its platform.

## API version

`IG_GRAPH_API_VERSION` defaults to `v25.0` and can be changed from Setup when
Meta deprecates that version.
