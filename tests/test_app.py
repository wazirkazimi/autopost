import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import app as reelposter


class ReelPosterTests(unittest.TestCase):
    def setUp(self):
        reelposter.app.config["TESTING"] = True
        with reelposter.jobs_lock:
            reelposter.jobs.clear()
        self.client = reelposter.app.test_client()

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(response.get_json()["job_queue"])
        self.assertTrue(response.get_json()["job_overview"])
        self.assertIn("telegram", response.get_json())

    def test_remote_web_password_protects_app_but_not_health(self):
        with patch.dict(
            os.environ,
            {
                "REELPOSTER_WEB_USERNAME": "reelposter",
                "REELPOSTER_WEB_PASSWORD": "test-password",
            },
        ):
            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            response = self.client.get(
                "/",
                headers={
                    "Authorization": "Basic cmVlbHBvc3Rlcjp0ZXN0LXBhc3N3b3Jk"
                },
            )
            self.assertEqual(response.status_code, 200)

    def test_instagram_webhook_verification_is_public(self):
        with patch.dict(
            os.environ,
            {
                "REELPOSTER_WEB_PASSWORD": "test-password",
                "META_WEBHOOK_VERIFY_TOKEN": "verify-me",
            },
        ):
            response = self.client.get(
                "/webhooks/instagram",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "verify-me",
                    "hub.challenge": "challenge-value",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "challenge-value")

    def test_instagram_webhook_rejects_invalid_signature(self):
        with patch.dict(os.environ, {"META_APP_SECRET": "app-secret"}):
            response = self.client.post(
                "/webhooks/instagram",
                data=b'{"object":"instagram"}',
                content_type="application/json",
                headers={"X-Hub-Signature-256": "sha256=invalid"},
            )
        self.assertEqual(response.status_code, 403)

    def test_instagram_webhook_queues_allowlisted_reel_once(self):
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "111"},
                            "recipient": {"id": "222"},
                            "message": {
                                "mid": "message-1",
                                "text": "Please post this",
                                "attachments": [
                                    {
                                        "type": "ig_reel",
                                        "payload": {
                                            "url": (
                                                "https://www.instagram.com/"
                                                "reel/ABC123/"
                                            )
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            ],
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(
            b"app-secret",
            raw,
            hashlib.sha256,
        ).hexdigest()
        with (
            patch.dict(
                os.environ,
                {
                    "META_APP_SECRET": "app-secret",
                    "IG_DM_ALLOWED_SENDER_IDS": "111",
                },
            ),
            patch.object(reelposter, "record_dm_sender"),
            patch.object(reelposter.executor, "submit") as submit,
        ):
            first = self.client.post(
                "/webhooks/instagram",
                data=raw,
                content_type="application/json",
                headers={"X-Hub-Signature-256": signature},
            )
            second = self.client.post(
                "/webhooks/instagram",
                data=raw,
                content_type="application/json",
                headers={"X-Hub-Signature-256": signature},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["accepted"], 1)
        self.assertEqual(second.get_json()["accepted"], 0)
        saved = next(iter(reelposter.jobs.values()))
        self.assertEqual(saved["intake_source"], "instagram_dm")
        self.assertTrue(saved["review_required"])
        self.assertEqual(saved["dm_sender_id"], "111")
        submit.assert_called_once()

    def test_unknown_dm_sender_is_recorded_but_not_queued(self):
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "999"},
                            "recipient": {"id": "222"},
                            "message": {
                                "mid": "message-unknown",
                                "text": (
                                    "https://www.instagram.com/reel/ABC123/"
                                ),
                            },
                        }
                    ]
                }
            ],
        }
        with (
            patch.dict(
                os.environ,
                {"IG_DM_ALLOWED_SENDER_IDS": "111"},
            ),
            patch.object(reelposter, "record_dm_sender") as record,
        ):
            accepted = reelposter.ingest_instagram_webhook(payload)
        self.assertEqual(accepted, 0)
        self.assertFalse(reelposter.jobs)
        record.assert_called_with("999")

    def test_direct_dm_video_uses_background_download_worker(self):
        with (
            patch.dict(
                os.environ,
                {"IG_DM_ALLOWED_SENDER_IDS": "111"},
            ),
            patch.object(reelposter, "record_dm_sender"),
            patch.object(reelposter.executor, "submit") as submit,
        ):
            result = reelposter.create_dm_submission(
                sender_id="111",
                recipient_id="222",
                message_id="direct-video-1",
                attachment_type="video",
                media_url="https://scontent.cdninstagram.com/video.mp4",
                message_text="Use this caption",
            )
        self.assertEqual(result["status"], "queued")
        self.assertEqual(
            submit.call_args.args[0],
            reelposter.prepare_direct_dm_media,
        )
        self.assertEqual(
            reelposter.jobs[result["id"]]["caption"],
            "Use this caption",
        )

    def test_direct_dm_worker_downloads_video_into_ready_review_job(self):
        response = Mock()
        response.url = "https://scontent.cdninstagram.com/video.mp4"
        response.headers = {
            "content-type": "video/mp4",
            "content-length": "8",
        }
        response.iter_content.return_value = [b"video123"]
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as temporary:
            jobs_dir = Path(temporary)
            job_id = "direct-worker-job"
            with reelposter.jobs_lock:
                reelposter.jobs[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "active_stage": "download",
                    "source_url": response.url,
                    "caption": "",
                    "events": [],
                    "intake_source": "instagram_dm",
                    "review_required": True,
                    "created_at": reelposter.utc_now(),
                    "updated_at": reelposter.utc_now(),
                }
            with (
                patch.object(reelposter, "JOBS_DIR", jobs_dir),
                patch.object(reelposter.requests, "get", return_value=response),
            ):
                reelposter.prepare_direct_dm_media(
                    job_id,
                    response.url,
                    "DM caption",
                )
            saved = reelposter.jobs[job_id]
            self.assertEqual(saved["status"], "ready")
            self.assertEqual(saved["caption"], "DM caption")
            self.assertTrue(Path(saved["source_path"]).is_file())
        response.close.assert_called_once()

    def test_instagram_login_token_uses_instagram_graph_host(self):
        self.assertEqual(
            reelposter.graph_api_base("IGAA-test-token"),
            "https://graph.instagram.com",
        )

    def test_access_token_normalization(self):
        self.assertEqual(
            reelposter.normalize_access_token(' IG_ACCESS_TOKEN="IGAA abc\n123" '),
            "IGAAabc123",
        )

    def test_failed_to_decrypt_has_actionable_token_error(self):
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.json.return_value = {
            "error": {"message": "Failed to decrypt"}
        }
        with self.assertRaises(reelposter.ReelPosterError) as error:
            reelposter.parse_graph_response(response)
        self.assertIn("invalid or has been revoked", str(error.exception))

    def test_schedule_parser_accepts_future_utc_time(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        parsed = reelposter.parse_schedule_at(future.isoformat())
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertGreater(parsed, datetime.now(timezone.utc))

    def test_schedule_parser_rejects_past_time(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        with self.assertRaises(reelposter.ReelPosterError):
            reelposter.parse_schedule_at(past.isoformat())

    def test_schedule_marks_job_and_starts_timer(self):
        job_id = "scheduled-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "uploading",
                "active_stage": "upload",
                "source_url": "",
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        with patch("app.threading.Timer") as timer:
            scheduled = reelposter.schedule_instagram_publish(job_id, future, 0)
        self.assertTrue(scheduled)
        self.assertEqual(reelposter.jobs[job_id]["status"], "scheduled")
        timer.return_value.start.assert_called_once()

    @patch("app.set_key")
    def test_instagram_login_auto_corrects_user_id(self, set_key_mock):
        settings = {
            "IG_ACCESS_TOKEN": "IGAA-test",
            "IG_USER_ID": "wrong-id",
        }
        with patch.object(
            reelposter,
            "graph_get",
            return_value={"user_id": "correct-id", "username": "updated"},
        ):
            reelposter.verify_instagram_credentials(settings)
        self.assertEqual(settings["IG_USER_ID"], "correct-id")
        set_key_mock.assert_called_once()

    def test_resolve_account_derives_instagram_login_user_id(self):
        profile = {
            "id": "environment",
            "name": "Environment account",
            "CLOUDINARY_CLOUD_NAME": "cloud",
            "CLOUDINARY_API_KEY": "key",
            "CLOUDINARY_API_SECRET": "secret",
            "IG_USER_ID": "",
            "IG_ACCESS_TOKEN": "IGAA-test",
        }
        with (
            patch.object(
                reelposter,
                "account_profiles",
                return_value=[profile],
            ),
            patch.object(
                reelposter,
                "graph_get",
                return_value={"user_id": "derived-id", "username": "main"},
            ),
            patch.dict(os.environ, {}, clear=False),
        ):
            settings = reelposter.resolve_account_settings("environment")
        self.assertEqual(settings["IG_USER_ID"], "derived-id")

    def test_public_video_preflight_accepts_partial_mp4(self):
        response = Mock()
        response.status_code = 206
        response.headers = {"content-type": "video/mp4"}
        with patch.object(reelposter.requests, "get", return_value=response):
            reelposter.validate_public_video_url("https://example.com/video.mp4")

    def test_watermark_preserves_logo_aspect_ratio(self):
        completed = Mock(returncode=0, stderr="")
        with (
            patch.object(reelposter, "ffmpeg_executable", return_value="ffmpeg"),
            patch.object(reelposter, "probe_video_dimensions", return_value=(720, 1280)),
            patch.object(reelposter.subprocess, "run", return_value=completed) as run,
        ):
            reelposter.watermark_video(
                Path("source.mp4"),
                Path("logo.png"),
                Path("output.mp4"),
                22,
                80,
                36,
            )
        command = run.call_args.args[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=158:-1", filter_graph)
        self.assertNotIn("scale2ref", filter_graph)
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertEqual(command[command.index("-preset") + 1], "veryfast")

    def test_cloudinary_upload_uses_small_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "output.mp4"
            video_path.write_bytes(b"video")
            settings = {
                "CLOUDINARY_CLOUD_NAME": "cloud",
                "CLOUDINARY_API_KEY": "key",
                "CLOUDINARY_API_SECRET": "secret",
            }
            with (
                patch.object(reelposter.cloudinary, "config"),
                patch.object(
                    reelposter.cloudinary.uploader,
                    "upload_large",
                    return_value={"secure_url": "https://example.com/video.mp4"},
                ) as upload,
            ):
                result = reelposter.upload_to_cloudinary(
                    video_path,
                    settings,
                    "job-id",
                )
        self.assertEqual(result, "https://example.com/video.mp4")
        self.assertEqual(upload.call_args.kwargs["chunk_size"], 6 * 1024 * 1024)

    def test_analytics_builds_totals_and_best_observed_time(self):
        settings = {
            "ACCOUNT_ID": "environment",
            "ACCOUNT_NAME": "Main account",
            "IG_USER_ID": "123",
            "IG_ACCESS_TOKEN": "IGAA-test",
        }
        media = {
            "data": [
                {
                    "id": "reel-1",
                    "caption": "First reel",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": "2026-06-01T18:30:00+00:00",
                    "permalink": "https://www.instagram.com/reel/one/",
                    "like_count": 8,
                    "comments_count": 2,
                },
                {
                    "id": "reel-2",
                    "caption": "Second reel",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": "2026-06-08T19:15:00+00:00",
                    "permalink": "https://www.instagram.com/reel/two/",
                    "like_count": 12,
                    "comments_count": 3,
                },
            ]
        }
        insight_values = {
            "reel-1": {
                "views": 1000,
                "reach": 800,
                "likes": 80,
                "comments": 10,
                "saved": 8,
                "shares": 12,
                "total_interactions": 110,
            },
            "reel-2": {
                "views": 2000,
                "reach": 1500,
                "likes": 160,
                "comments": 20,
                "saved": 15,
                "shares": 25,
                "total_interactions": 220,
            },
        }
        with (
            patch.dict(os.environ, {"APP_TIMEZONE": "UTC"}),
            patch.object(
                reelposter,
                "resolve_account_settings",
                return_value=settings,
            ),
            patch.object(
                reelposter,
                "graph_get",
                side_effect=[
                    {
                        "username": "main",
                        "followers_count": 500,
                        "media_count": 20,
                    },
                    media,
                ],
            ),
            patch.object(
                reelposter,
                "fetch_media_insights",
                side_effect=lambda media_id, _token: insight_values[media_id],
            ),
        ):
            payload = reelposter.build_instagram_analytics()

        self.assertEqual(payload["analyzed_count"], 2)
        self.assertEqual(payload["totals"]["views"], 3000)
        self.assertEqual(payload["totals"]["total_interactions"], 330)
        self.assertEqual(payload["best_times"][0]["label"], "Monday, 6 PM-9 PM")
        self.assertEqual(payload["best_times"][0]["sample_count"], 2)

    def test_analytics_endpoint_returns_selected_account_data(self):
        payload = {
            "account": {"id": "account-2", "name": "Second account"},
            "totals": {},
            "best_times": [],
            "media": [],
        }
        with patch.object(
            reelposter,
            "instagram_analytics",
            return_value=payload,
        ) as analytics:
            response = self.client.get(
                "/api/analytics?account_id=account-2&refresh=1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["account"]["id"], "account-2")
        analytics.assert_called_once_with("account-2", refresh=True)

    def test_jobs_endpoint_returns_newest_first(self):
        with reelposter.jobs_lock:
            reelposter.jobs["older"] = {
                "id": "older",
                "status": "done",
                "active_stage": None,
                "source_url": "",
                "caption": "",
                "events": [],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            reelposter.jobs["newer"] = {
                "id": "newer",
                "status": "posting",
                "active_stage": "post",
                "source_url": "",
                "caption": "",
                "events": [],
                "created_at": "2026-01-02T00:00:00+00:00",
                "updated_at": "2026-01-02T00:00:00+00:00",
            }
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        ids = [job["id"] for job in response.get_json()["jobs"]]
        self.assertLess(ids.index("newer"), ids.index("older"))

    def test_public_job_includes_thumbnail_url(self):
        job = {
            "id": "thumbnail-job",
            "status": "ready",
            "active_stage": None,
            "source_path": "source.mp4",
            "source_url": "",
            "caption": "",
            "events": [],
            "created_at": reelposter.utc_now(),
            "updated_at": reelposter.utc_now(),
        }
        payload = reelposter.public_job(job)
        self.assertEqual(
            payload["thumbnail_url"],
            "/api/jobs/thumbnail-job/thumbnail",
        )
        self.assertFalse(payload["hide_counts_requested"])

    def test_prepare_requires_rights_confirmation(self):
        response = self.client.post(
            "/api/reels/prepare",
            json={"url": "https://www.youtube.com/shorts/ABC123"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("own the video", response.get_json()["error"])

    def test_universal_source_validation(self):
        examples = {
            "https://www.instagram.com/reel/ABC123/": "instagram",
            "https://www.youtube.com/watch?v=ABC123": "youtube",
            "https://youtu.be/ABC123": "youtube",
            "https://www.youtube.com/shorts/ABC123": "youtube",
            "https://www.reddit.com/r/videos/comments/abc123/title/": "reddit",
            "https://v.redd.it/abc123": "reddit",
            "https://x.com/example/status/123456": "x",
            "https://twitter.com/example/status/123456": "x",
        }
        for url, expected in examples.items():
            with self.subTest(url=url):
                _normalized, platform = reelposter.source_platform_for_url(url)
                self.assertEqual(platform, expected)

    def test_universal_source_rejects_arbitrary_and_playlist_urls(self):
        with self.assertRaises(reelposter.ReelPosterError):
            reelposter.source_platform_for_url("https://example.com/video.mp4")
        with self.assertRaises(reelposter.ReelPosterError):
            reelposter.source_platform_for_url(
                "https://www.youtube.com/playlist?list=ABC123"
            )

    def test_source_media_filter_rejects_unsupported_media(self):
        self.assertIn(
            "15-minute",
            reelposter.source_media_match_filter(
                {"duration": 901},
                incomplete=False,
            ),
        )
        self.assertIn(
            "450 MB",
            reelposter.source_media_match_filter(
                {"filesize": reelposter.MAX_SOURCE_MEDIA_BYTES + 1},
                incomplete=False,
            ),
        )
        self.assertIn(
            "Playlists",
            reelposter.source_media_match_filter(
                {"_type": "playlist"},
                incomplete=False,
            ),
        )

    def test_caption_attribution_respects_instagram_limit(self):
        credit = reelposter.source_attribution_text(
            "youtube",
            "Creator",
            "https://youtu.be/ABC123",
        )
        self.assertIn("Creator on YouTube", credit)
        self.assertIn("https://youtu.be/ABC123", credit)
        combined = reelposter.caption_with_attribution("Caption", credit)
        self.assertIn("Caption\n\nSource:", combined)
        with self.assertRaises(reelposter.ReelPosterError):
            reelposter.caption_with_attribution("x" * 2200, credit)

    def test_prepare_creates_background_job(self):
        with patch.object(reelposter.executor, "submit") as submit:
            response = self.client.post(
                "/api/reels/prepare",
                json={
                    "url": "https://www.youtube.com/shorts/ABC123",
                    "rights_confirmed": True,
                },
            )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["source_platform"], "youtube")
        self.assertTrue(payload["rights_confirmed"])
        self.assertTrue(payload["include_attribution"])
        self.assertEqual(payload["stages"][0]["state"], "active")
        submit.assert_called_once()

    def test_prepare_helper_keeps_telegram_metadata(self):
        with patch.object(reelposter.executor, "submit"):
            payload = reelposter.create_prepare_job(
                "https://www.instagram.com/reel/ABC123/",
                {
                    "telegram_chat_id": 123,
                    "telegram_user_id": 456,
                    "status": "done",
                },
            )
        saved = reelposter.get_job_or_404(payload["id"])
        self.assertEqual(saved["telegram_chat_id"], 123)
        self.assertEqual(saved["telegram_user_id"], 456)
        self.assertEqual(saved["status"], "queued")

    def test_account_profile_endpoint_saves_without_returning_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "accounts.json"
            with (
                patch.object(reelposter, "ACCOUNTS_STATE_PATH", state_path),
                patch.object(reelposter, "environment_account", return_value=None),
            ):
                response = self.client.post(
                    "/api/accounts",
                    data={
                        "name": "Second account",
                        "CLOUDINARY_CLOUD_NAME": "cloud",
                        "CLOUDINARY_API_KEY": "key",
                        "CLOUDINARY_API_SECRET": "secret",
                        "IG_USER_ID": "123456",
                        "IG_ACCESS_TOKEN": "IGAA-test-token-value",
                    },
                )
                listed = self.client.get("/api/accounts").get_json()["accounts"]
        self.assertEqual(response.status_code, 201)
        self.assertEqual(listed[0]["name"], "Second account")
        self.assertNotIn("IG_ACCESS_TOKEN", response.get_json()["account"])
        self.assertTrue(state_path.exists() or listed)

    def test_overlay_endpoint_adds_selectable_gif(self):
        with tempfile.TemporaryDirectory() as temporary:
            overlay_dir = Path(temporary) / "overlays"
            overlay_dir.mkdir()
            state_path = Path(temporary) / "overlays.json"
            with (
                patch.object(reelposter, "OVERLAYS_DIR", overlay_dir),
                patch.object(reelposter, "OVERLAYS_STATE_PATH", state_path),
                patch.object(reelposter, "current_logo_path", return_value=None),
            ):
                response = self.client.post(
                    "/api/overlays",
                    data={
                        "name": "Animated mark",
                        "overlay": (io.BytesIO(b"GIF89a"), "mark.gif"),
                    },
                    content_type="multipart/form-data",
                )
                listed = self.client.get("/api/overlays").get_json()["overlays"]
        self.assertEqual(response.status_code, 201)
        self.assertEqual(listed[0]["name"], "Animated mark")
        self.assertTrue(listed[0]["animated"])

    def test_post_validates_caption_length(self):
        job_id = "test-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "ready",
                "active_stage": None,
                "source_path": str(Path(tempfile.gettempdir()) / "source.mp4"),
                "source_url": "https://www.instagram.com/reel/ABC123/",
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        response = self.client.post(
            f"/api/reels/{job_id}/post",
            json={"caption": "x" * 2201},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("2,200", response.get_json()["error"])

    def test_post_rejects_invalid_destination(self):
        job_id = "destination-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "ready",
                "active_stage": None,
                "source_path": str(Path(tempfile.gettempdir()) / "source.mp4"),
                "source_url": "https://www.instagram.com/reel/ABC123/",
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        response = self.client.post(
            f"/api/reels/{job_id}/post",
            json={
                "placement_mode": "center-v2",
                "x_center_percent": 80,
                "y_center_percent": 36,
                "destination": "stories",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("destination", response.get_json()["error"])

    def test_post_passes_center_based_logo_coordinates(self):
        job_id = "placement-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "ready",
                "active_stage": None,
                "source_path": str(Path(tempfile.gettempdir()) / "source.mp4"),
                "source_url": "https://www.instagram.com/reel/ABC123/",
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        with patch.object(reelposter.executor, "submit") as submit:
            response = self.client.post(
                f"/api/reels/{job_id}/post",
                json={
                    "placement_mode": "center-v2",
                    "x_center_percent": 24.5,
                    "y_center_percent": 31.25,
                    "size_percent": 18,
                    "destination": "grid",
                },
            )
        self.assertEqual(response.status_code, 202)
        args = submit.call_args.args
        self.assertEqual(args[4], 24.5)
        self.assertEqual(args[5], 31.25)

    def test_post_passes_selected_account_and_overlay(self):
        job_id = "profile-selection-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "ready",
                "active_stage": None,
                "source_path": str(Path(tempfile.gettempdir()) / "source.mp4"),
                "source_url": "https://www.instagram.com/reel/ABC123/",
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        account = {
            "ACCOUNT_ID": "account-2",
            "ACCOUNT_NAME": "Second account",
        }
        overlay = {"id": "overlay-2", "name": "Blue mark"}
        with (
            patch.object(reelposter, "resolve_account_settings", return_value=account),
            patch.object(
                reelposter,
                "resolve_overlay_path",
                return_value=(Path("mark.png"), overlay),
            ),
            patch.object(reelposter.executor, "submit") as submit,
        ):
            response = self.client.post(
                f"/api/reels/{job_id}/post",
                json={
                    "placement_mode": "center-v2",
                    "x_center_percent": 80,
                    "y_center_percent": 36,
                    "size_percent": 16,
                    "destination": "grid",
                    "account_id": "account-2",
                    "overlay_id": "overlay-2",
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(submit.call_args.args[-2:], ("account-2", "overlay-2"))
        self.assertEqual(reelposter.jobs[job_id]["account_name"], "Second account")
        self.assertEqual(reelposter.jobs[job_id]["overlay_name"], "Blue mark")

    def test_hide_counts_preference_is_off_by_default_and_can_be_requested(self):
        job_id = "hide-counts-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "ready",
                "active_stage": None,
                "source_path": str(Path(tempfile.gettempdir()) / "source.mp4"),
                "source_url": "https://www.instagram.com/reel/ABC123/",
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        account = {
            "ACCOUNT_ID": "environment",
            "ACCOUNT_NAME": "Environment account",
        }
        overlay = {"id": "default", "name": "Default overlay"}
        with (
            patch.object(reelposter, "resolve_account_settings", return_value=account),
            patch.object(
                reelposter,
                "resolve_overlay_path",
                return_value=(Path("mark.png"), overlay),
            ),
            patch.object(reelposter.executor, "submit"),
        ):
            response = self.client.post(
                f"/api/reels/{job_id}/post",
                json={
                    "placement_mode": "center-v2",
                    "x_center_percent": 80,
                    "y_center_percent": 36,
                    "destination": "grid",
                    "hide_counts": True,
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(reelposter.jobs[job_id]["hide_counts_requested"])
        self.assertFalse(
            reelposter.jobs[job_id]["manual_count_hiding_required"]
        )

    def test_non_instagram_post_requires_rights_and_can_add_attribution(self):
        job_id = "youtube-rights-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "ready",
                "active_stage": None,
                "source_path": str(Path(tempfile.gettempdir()) / "source.mp4"),
                "source_url": "https://youtu.be/ABC123",
                "source_platform": "youtube",
                "source_attribution": (
                    "Source: Creator on YouTube\nhttps://youtu.be/ABC123"
                ),
                "rights_confirmed": False,
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        account = {
            "ACCOUNT_ID": "environment",
            "ACCOUNT_NAME": "Environment account",
        }
        overlay = {"id": "default", "name": "Default overlay"}
        with (
            patch.object(reelposter, "resolve_account_settings", return_value=account),
            patch.object(
                reelposter,
                "resolve_overlay_path",
                return_value=(Path("mark.png"), overlay),
            ),
            patch.object(reelposter.executor, "submit") as submit,
        ):
            rejected = self.client.post(
                f"/api/reels/{job_id}/post",
                json={
                    "placement_mode": "center-v2",
                    "x_center_percent": 80,
                    "y_center_percent": 36,
                    "destination": "grid",
                },
            )
            accepted = self.client.post(
                f"/api/reels/{job_id}/post",
                json={
                    "caption": "My caption",
                    "placement_mode": "center-v2",
                    "x_center_percent": 80,
                    "y_center_percent": 36,
                    "destination": "grid",
                    "rights_confirmed": True,
                    "include_attribution": True,
                },
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("permission", rejected.get_json()["error"])
        self.assertEqual(accepted.status_code, 202)
        self.assertIn(
            "Source: Creator on YouTube",
            reelposter.jobs[job_id]["caption"],
        )
        submit.assert_called_once()

    def test_reel_container_uses_only_documented_visibility_parameters(self):
        job = {
            "cloudinary_url": "https://example.com/video.mp4",
            "caption": "Caption",
            "share_to_feed": True,
            "hide_counts_requested": True,
        }
        settings = {
            "IG_USER_ID": "123",
            "IG_ACCESS_TOKEN": "IGAA-test",
        }
        with (
            patch.object(
                reelposter,
                "graph_post",
                return_value={"id": "container-id"},
            ) as graph_post,
            patch.object(reelposter, "wait_for_container"),
        ):
            result = reelposter.create_and_process_container(
                "container-job",
                job,
                settings,
            )
        payload = graph_post.call_args.args[1]
        self.assertEqual(result, "container-id")
        self.assertEqual(payload["share_to_feed"], "true")
        self.assertNotIn("hide_like_and_view_counts", payload)
        self.assertNotIn("hide_share_count", payload)

    def test_post_rejects_outdated_placement_contract(self):
        job_id = "outdated-placement-job"
        with reelposter.jobs_lock:
            reelposter.jobs[job_id] = {
                "id": job_id,
                "status": "ready",
                "active_stage": None,
                "source_path": str(Path(tempfile.gettempdir()) / "source.mp4"),
                "source_url": "https://www.instagram.com/reel/ABC123/",
                "caption": "",
                "events": [],
                "created_at": reelposter.utc_now(),
                "updated_at": reelposter.utc_now(),
            }
        response = self.client.post(
            f"/api/reels/{job_id}/post",
            json={
                "x_center_percent": 80,
                "y_center_percent": 36,
                "destination": "grid",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("out of date", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
