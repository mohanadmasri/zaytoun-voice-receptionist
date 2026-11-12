import os
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.main import app, get_gateway
from core.session_store import session_store
from test_voice_api import FakeGateway, wav_bytes


class OpenRouterSTTTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_completion_never_reaches_booking(self):
        gateway = FakeGateway()
        with patch.dict(app.dependency_overrides, {get_gateway: lambda: gateway}):
            await self._check_incomplete_completions()
        self.assertEqual(gateway.writes, [])

    async def _check_incomplete_completions(self):
        for reason in ("length", "content_filter"):
            with self.subTest(reason=reason):
                session_id = f"stt-incomplete-{uuid4().hex}"
                before = deepcopy(session_store.get_or_create(session_id))
                before.update(selected_slot_id="test-slot", patient_name="Lina Mansour")
                session_store.save(deepcopy(before))
                self.addCleanup(session_store.delete, session_id)
                self.addCleanup(app.state.session_locks.pop, session_id, None)
                self.addCleanup(app.state.voice_generations.pop, session_id, None)
                self.addCleanup(app.state.voice_reviews.pop, session_id, None)
                self.addCleanup(app.state.last_audio.pop, session_id, None)
                upstream = httpx.Response(200, json={"choices": [{
                    "finish_reason": reason,
                    "message": {"content": "رقم تلفوني 0599000111"},
                }]})
                with (
                    patch.dict(os.environ, {
                        "STT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key",
                    }),
                    patch("voice.stt.httpx.post", return_value=upstream) as post,
                    patch("app.main.process_message") as process,
                ):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test",
                    ) as client:
                        response = await client.post("/api/voice", data={
                            "session_id": session_id, "language_hint": "ar", "preview_only": "true",
                        }, files={"audio": ("recording.wav", wav_bytes(), "audio/wav")})
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["transcript"], "")
                self.assertIsNone(response.json().get("transcript_token"))
                self.assertEqual(session_store.get_or_create(session_id), before)
                process.assert_not_called()
                post.assert_called_once()

    async def test_provider_503_keeps_session_and_allows_manual_retry(self):
        session = uuid4().hex
        gateway = FakeGateway()
        before = deepcopy(session_store.get_or_create(session))
        before.update(language="ar", service_id="dental_checkup", stage="awaiting_details")
        session_store.save(deepcopy(before))
        self.addCleanup(session_store.delete, session)
        self.addCleanup(app.state.last_audio.pop, session, None)
        self.addCleanup(app.state.voice_generations.pop, session, None)
        self.addCleanup(app.state.voice_reviews.pop, session, None)
        self.addCleanup(app.state.session_locks.pop, session, None)
        unavailable = httpx.Response(503, text="private upstream response")
        success = httpx.Response(200, json={"choices": [{"message": {"content": "أقرب موعد"}}]})
        with (
            patch.dict(app.dependency_overrides, {get_gateway: lambda: gateway}),
            patch.dict(os.environ, {"STT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key"}),
            patch("voice.stt.httpx.post", side_effect=[unavailable, unavailable, success]) as post,
            patch("voice.stt.time.sleep"),
            patch("app.main.process_message") as process,
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                async def record():
                    return await client.post("/api/voice", data={
                        "session_id": session, "language_hint": "ar", "preview_only": "true",
                    }, files={"audio": ("recording.wav", wav_bytes(), "audio/wav")})
                with self.assertLogs("uvicorn.error", level="INFO") as logs:
                    failed = await record()
                self.assertEqual(failed.status_code, 503)
                self.assertEqual(failed.json().get("category"), "stt_provider_error")
                self.assertIn("إعادة التسجيل", failed.json()["reply"])
                self.assertEqual(failed.json()["transcript"], "")
                self.assertEqual(session_store.get_or_create(session), before)
                self.assertEqual(post.call_count, 2)
                self.assertIn('"stt_upstream_status":503', logs.output[-1])
                self.assertNotIn("private upstream response", logs.output[-1])
                recovered = await record()
                self.assertEqual(recovered.status_code, 200, recovered.text)
                self.assertEqual(recovered.json()["transcript"], "أقرب موعد")
                self.assertEqual(post.call_count, 3)
                process.assert_not_called()
