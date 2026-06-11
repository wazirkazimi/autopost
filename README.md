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

## Instagram DM intake

ReelPoster can receive Reel submissions through Instagram Messaging and place
them in an approval queue. It never publishes directly from a DM.

1. Deploy ReelPoster to Render and set `META_WEBHOOK_VERIFY_TOKEN` to a long
   random value.
2. Set `META_APP_SECRET` to the App Secret from the Meta developer dashboard.
3. In the Meta app's Instagram webhook settings, use:
   `https://YOUR-RENDER-DOMAIN/webhooks/instagram`
4. Enter the same verify token and subscribe the connected Instagram
   professional account to message webhook events.
5. Send a Reel or video to the account by DM. The sender's Instagram-scoped ID
   appears in ReelPoster Setup as **Seen, not allowed**.
6. Add that ID to **Allowed Instagram DM sender IDs**, save Setup, and ask the
   sender to send the Reel again.
7. Open Jobs and click **Review & edit**. Adjust the caption, account, overlay,
   position, destination, delay, or schedule before publishing.

Webhook requests are validated with `X-Hub-Signature-256` using the Meta App
Secret. Unknown senders are recorded for discovery but their media is ignored.
Duplicate webhook deliveries are ignored by message ID. Direct attachment
downloads are restricted to trusted Meta and Instagram domains and capped at
500 MB.

Public Reel URLs and downloadable video attachments are supported. Ephemeral
media and private shares without an accessible media URL cannot be downloaded;
ask the sender to send the video file or a public Reel URL instead. Production
use with people outside your Meta app's roles can require Meta App Review and
the relevant Instagram messaging and content-publishing permissions.

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
The free Render Blueprint uses ephemeral storage, so this directory can be
cleared whenever the service sleeps, restarts, or redeploys. API responses never
return saved access tokens or Cloudinary secrets.

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

The included `render.yaml` uses a free Render web service. This is suitable for
testing and immediate/manual posts, but Render can spin it down after 15 minutes
without inbound traffic. Free services also have ephemeral storage, so logos,
saved profiles, downloaded media, and job history can disappear after a sleep,
restart, or deployment. Delayed posts, exact schedules, and Telegram long
polling are therefore not reliable on the free plan.

1. Push this repository to a private GitHub repository.
2. In Render, choose **New > Blueprint** and select the repository.
3. Render reads `render.yaml` and asks for the secret environment values.
4. Enter the Cloudinary, Instagram, Telegram, and Meta webhook values from
   `.env.example`.
   Set `REELPOSTER_WEB_PASSWORD` to a long, unique password. The username
   defaults to `reelposter`.
5. Deploy, open the Render URL once, and upload the logo on the Setup page.

The deployment deliberately uses one Gunicorn worker. Telegram long polling
runs inside that process; additional workers would start duplicate bot
instances. For reliable scheduling and persistent uploads, change the service
to a paid instance and attach a disk at `/opt/render/project/src/data`.

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
