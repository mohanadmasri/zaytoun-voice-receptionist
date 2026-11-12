# تفحص ربط واجهة Gradio الموجودة بالصوت والنص والموافقة دون تشغيل خدمات خارجية.
# المدخل ردود HTTP وهمية وملف WAV من العينات؛ المخرج حقول الواجهة ومعرفات الجلسة.
# لا تسجل ميكروفونًا فعليًا ولا تنطق أو تحجز؛ التشغيل اليدوي موضح في VOICE_DEMO.md.
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

with patch.dict(os.environ, {"GRADIO_ANALYTICS_ENABLED": "False"}):
    from ui import app_ui


# تختبر handlers الحقيقية للواجهة مع استبدال حد HTTP فقط ببيانات ثابتة.
class VoiceUiTests(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(session_hash="voice-ui-test")
        self.response = {"session_id": "ui-voice-ui-test", "reply": "1. Test appointment",
                         "transcript": "option two", "state": "awaiting_approval", "tool_calls": [],
                         "audio_b64": None, "audio_mime": None,
                         "pending_action": {"action_id": "test-action", "type": "create_booking",
                                            "summary": "Test appointment", "payload": {}}}

    def test_recording_text_and_approval_share_session(self):
        calls = []

        # تفحص multipart أثناء فتح الملف وتعيد ردًا وهميًا دون إرسال أي طلب شبكي.
        def fake_post(path, **kwargs):
            calls.append((path, kwargs))
            if path == "/api/voice":
                filename, recording, mime = kwargs["files"]["audio"]
                self.assertTrue(recording.read(4) == b"RIFF")
                self.assertEqual(mime, "audio/wav")
            return self.response

        with patch("ui.app_ui.post", side_effect=fake_post):
            outputs = app_ui.send_voice("samples/audio/en_02_vague_friday.wav", [], self.request)
            self.assertEqual(outputs[1], "option two")
            self.assertEqual(outputs[3], self.response["pending_action"])
            app_ui.send_text("option 2", [], self.request)
            app_ui.reject(self.response["pending_action"], [], self.request)
        self.assertEqual([path for path, _ in calls], ["/api/voice", "/api/chat", "/api/approve"])
        self.assertEqual(calls[0][1]["data"]["session_id"], calls[1][1]["json"]["session_id"])
        self.assertEqual(calls[1][1]["json"]["session_id"], calls[2][1]["json"]["session_id"])
        self.assertIs(calls[2][1]["json"]["approved"], False)

    def test_structured_voice_error_keeps_pending_controls(self):
        data = {**self.response, "reply": "Sorry, I didn't catch that.", "transcript": ""}
        with patch("ui.app_ui.httpx.post", return_value=httpx.Response(422, json=data)):
            received = app_ui.post("/api/voice")
        self.assertEqual(received, data)
        outputs = app_ui.show(received, [])
        self.assertEqual(outputs[3], self.response["pending_action"])
        self.assertTrue(outputs[4]["visible"])
        self.assertTrue(outputs[5]["visible"])

    def test_transcript_review_uses_token_without_resending_audio(self):
        preview = {**self.response, "needs_transcript_confirmation": True, "transcript_token": "review-token"}
        with patch("ui.app_ui.post", return_value=preview) as post:
            outputs = app_ui.send_voice("samples/audio/en_02_vague_friday.wav", [], self.request, "English")
            self.assertEqual(outputs[6], "review-token")
            self.assertEqual(post.call_args.args[0], "/api/voice")
        with patch("ui.app_ui.post", return_value=self.response) as post:
            outputs = app_ui.use_transcript("review-token", [], self.request)
            self.assertEqual(post.call_args.args[0], "/api/voice/confirm")
            self.assertEqual(post.call_args.kwargs["json"]["transcript_token"], "review-token")
            self.assertNotIn("files", post.call_args.kwargs)
            self.assertIsNone(outputs[6])

    def test_failed_recording_shows_error_without_fake_transcript(self):
        for status, reply in (
            (503, "خدمة الصوت مش متاحة هلأ. جرّب تكتب رسالتك."),
            (422, "التفريغ مش واضح أو ما بطابق لغة التسجيل."),
            (422, "The phone number was unclear. Please repeat the digits or type the number."),
            (504, "Speech recognition took too long. Please try again or type your message."),
        ):
            with self.subTest(status=status, reply=reply):
                data = {**self.response, "reply": reply, "transcript": ""}
                history = [{"role": "user", "content": "Previous turn"}]
                with patch("ui.app_ui.httpx.post", return_value=httpx.Response(status, json=data)):
                    outputs = app_ui.send_voice("samples/audio/en_02_vague_friday.wav", history, self.request)
                self.assertEqual(outputs[0], history + [{"role": "assistant", "content": reply}])
                self.assertEqual(outputs[1], "")
                self.assertEqual(outputs[3], self.response["pending_action"])
                self.assertIsNone(outputs[6])

    def test_microphone_format_and_network_failure(self):
        self.assertEqual(app_ui.mic.get_config()["format"], "wav")
        self.assertEqual(app_ui.mic.get_config()["sources"], ["microphone", "upload"])
        with patch("ui.app_ui.httpx.post", side_effect=httpx.ConnectError("private details")):
            response = app_ui.post("/api/chat")
        self.assertIn("Cannot reach", response["reply"])
        self.assertNotIn("private details", response["reply"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
