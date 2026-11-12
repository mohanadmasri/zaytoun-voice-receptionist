import os
import unittest
from unittest.mock import patch

import httpx

from agent.patient_identity import spoken_phone_digits
from core.phone import normalize_patient_phone
from integrations.voice_adapter import VoiceAdapter, VoiceError
from test_voice_api import wav_bytes


class ArabicPhoneStabilizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_arabic_phone_labels_and_punctuation_do_not_return_422(self):
        for transcript in ("رقم الموبايل هو: ٠٥٨٩٠٠١١١١.", "رقم تلفوني: ٠٥٨، ٩٠٠، ١١١١۔",
                           "رقم جوالي: ٠٥٨‑٩٠٠‑١١١١", "رقم الجوال: ۰۵۸۹۰۰۱۱۱۱",
                           "رقم الموبايل: صفر خمسة ثمانية تسعة صفر صفر واحد واحد واحد واحد"):
            with self.subTest(transcript=transcript), patch(
                "integrations.voice_adapter.transcribe", return_value=transcript
            ):
                result = await VoiceAdapter().transcribe(wav_bytes(), "ar", phone_mode=True)
                self.assertEqual(result, transcript)
                self.assertEqual(normalize_patient_phone(spoken_phone_digits(result)), "0589001111")

    async def test_invalid_numbers_remain_rejected_with_arabic_phone_label(self):
        for transcript in ("رقم الموبايل: ٠٥٨٩٠٠١١١", "رقم الموبايل: ٠٥٧٩٠٠١١١١",
                           "رقم الموبايل: ٠٥٨/٩٠٠١١١١", "رقم الموبايل: ٠٥٨٩٠٠١١١١ أو ٠٥٩٩٠٠١١١١"):
            with self.subTest(transcript=transcript), patch(
                "integrations.voice_adapter.transcribe", return_value=transcript
            ), self.assertRaises(VoiceError) as rejected:
                await VoiceAdapter().transcribe(wav_bytes(), "ar", phone_mode=True)
            self.assertEqual(rejected.exception.status_code, 422)


class STTStabilizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_in_band_errors_recover_using_same_model_and_audio(self):
        transcript = "بدي موعد لفحص أسنان"
        recovered = httpx.Response(200, json={"choices": [{"message": {"content": transcript}}]})
        for envelope in (
            {"error": {"code": "503"}},
            {"choices": [{"finish_reason": "error", "error": {"code": 503}, "message": {"content": "partial"}}]},
        ):
            with self.subTest(envelope=envelope), patch.dict(os.environ, {
                "STT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key", "STT_MODEL": "test-model",
                "STT_FALLBACK_MODEL": "",
            }), patch("voice.stt.time.sleep"), patch(
                "voice.stt.httpx.post", side_effect=[httpx.Response(200, json=envelope), recovered]
            ) as request:
                self.assertEqual(await VoiceAdapter().transcribe(wav_bytes(), "ar"), transcript)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args_list[0].kwargs["json"], request.call_args_list[1].kwargs["json"])

    async def test_permanent_choice_error_does_not_forward_partial_transcript(self):
        response = httpx.Response(200, json={
            "choices": [{"finish_reason": "error", "error": {"code": 401}, "message": {"content": "بدي موعد"}}],
        })
        with patch.dict(os.environ, {"STT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key"}), patch(
            "voice.stt.httpx.post", return_value=response
        ) as request, self.assertRaises(VoiceError) as rejected:
            await VoiceAdapter().transcribe(wav_bytes(), "ar")
        self.assertEqual(rejected.exception.status_code, 503)
        request.assert_called_once()

    async def test_provider_outage_after_retry_returns_503(self):
        response = httpx.Response(503)
        with patch.dict(os.environ, {"STT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key"}), patch(
            "voice.stt.time.sleep"
        ), patch("voice.stt.httpx.post", return_value=response) as request, self.assertRaises(VoiceError) as rejected:
            await VoiceAdapter().transcribe(wav_bytes(), "ar")
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(request.call_count, 2)
