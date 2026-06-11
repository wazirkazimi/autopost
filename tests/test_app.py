import io
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

    def test_prepare_rejects_non_instagram_url(self):
        response = self.client.post(
            "/api/reels/prepare",
            json={"url": "https://example.com/reel/not-instagram"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Instagram Reel URL", response.get_json()["error"])

    def test_prepare_creates_background_job(self):
        with patch.object(reelposter.executor, "submit") as submit:
            response = self.client.post(
                "/api/reels/prepare",
                json={"url": "https://www.instagram.com/reel/ABC123/"},
            )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["status"], "queued")
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
