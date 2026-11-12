# اختبارات معزولة لرفض العيادة وتسجيله المنقح؛ تمنع أي إنشاء حجز فعلي.
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import httpx
from integrations.mcp_gateway import ClinicGateway, sanitized_clinic_error
from agent.patient_identity import spoken_phone_digits, normalize_patient_phone, phone_validation_issue
from voice import stt
from test_voice_api import wav_bytes


class PhoneRepresentationTests(unittest.TestCase):
    # جميع الأرقام اصطناعية؛ لا نستنتج رقمًا مقصودًا من تفريغ غير مؤكد.
    def test_literal_digit_sequences_and_validation_reasons(self):
        for text in ("zero five nine nine zero zero zero one one one", "صفر خمسة تسعة تسعة صفر صفر صفر واحد واحد واحد", "٠٥٩٩٠٠٠١١١", "0599000111"):
            self.assertEqual(spoken_phone_digits(text), "0599000111")
        self.assertIsNone(spoken_phone_digits("five hundred maybe nine"))
        self.assertIsNone(normalize_patient_phone("599000111"))
        self.assertEqual(phone_validation_issue("599000111"), "prefix")
        self.assertEqual(phone_validation_issue("05990001111"), "length")
        self.assertEqual(phone_validation_issue("05?9000111"), "format")
        self.assertEqual(normalize_patient_phone("+٩٧٠٥٩٩٠٠٠١١١"), "0599000111")
        self.assertIsNone(normalize_patient_phone("058900111"))

    def test_phone_prompt_uses_context_without_priming_a_number(self):
        response = httpx.Response(200, json={"choices": [{"message": {"content": "0599000111"}}]})
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock-key"}), patch("voice.stt.httpx.post", return_value=response) as post:
            self.assertEqual(stt._openrouter(wav_bytes(), "wav", "ar", phone_mode=True), "0599000111")
        body = post.call_args.kwargs["json"]
        prompt = body["messages"][0]["content"][0]["text"]
        self.assertIn("each spoken digit", prompt)
        self.assertIn("Never fill inaudible digits", prompt)
        self.assertNotIn("0599000111", prompt)
        self.assertEqual(body["temperature"], 0)


class ClinicRejectionTests(unittest.IsolatedAsyncioTestCase):
    # نشغل نفس تطبيق العيادة داخل العملية، ونمنع دالة الكتابة حتى لو فشل شرط الاختبار.
    async def test_isolated_original_clinic_422_is_capacity_policy_not_phone_format(self):
        from services import clinic_api as clinic
        now = datetime(2026, 11, 2, 8, tzinfo=ZoneInfo("Asia/Jerusalem"))
        payload = {"slot_id": "SL-20261103-1630-dr_khoury", "service_id": "dental_checkup",
                   "patient": {"name": "Lina Haddad", "phone": "0599000111"}}
        with patch.object(clinic.STORE, "now", return_value=now), patch.object(clinic.STORE, "apply_fault", new=AsyncMock(return_value=None)), \
             patch.object(clinic.STORE, "upcoming_for", return_value=[{}, {}, {}]), \
             patch.object(clinic.STORE, "_add", side_effect=AssertionError("No booking writes allowed")) as add:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=clinic.app), base_url="http://isolated") as client:
                response = await client.post("/api/bookings", json=payload, headers={"X-API-Key": clinic.API_KEY})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_violation")
        self.assertIn("upcoming appointments", response.json()["error"]["message"])
        add.assert_not_called()
        detail = sanitized_clinic_error({"status": 422, **response.json()}, payload)
        self.assertIn("policies.max_upcoming_bookings_per_patient", detail["rejected_fields"])
        self.assertNotIn(payload["patient"]["phone"], str(detail))

    # نحاكي نقل MCP نفسه ونفحص سجل البوابة دون تشغيل خادم أو استخدام بيانات مريض حقيقية.
    async def test_gateway_logs_only_sanitized_rejection_fields(self):
        args = {"patient_name": "Lina Haddad", "patient_phone": "0599000111"}
        rejection = {"ok": False, "status": 422, "error": {"code": "policy_violation",
            "message": "patient.phone rejected for Lina Haddad: 0599000111"}}
        session = SimpleNamespace(initialize=AsyncMock(), call_tool=AsyncMock(return_value=SimpleNamespace(
            structuredContent=rejection, content=[], isError=False)))
        @asynccontextmanager
        async def transport(*_args, **_kwargs):
            yield (None, None)
        @asynccontextmanager
        async def connection(*_args, **_kwargs):
            yield session
        with patch("integrations.mcp_gateway.stdio_client", transport), patch("integrations.mcp_gateway.ClientSession", connection), \
             self.assertLogs("integrations.mcp_gateway", level="WARNING") as captured:
            result = await ClinicGateway().call_write("create_booking", args)
        self.assertEqual(result, rejection)
        log = " ".join(captured.output)
        for field in ("status=422", "code=policy_violation", "message=", "rejected_fields="):
            self.assertIn(field, log)
        self.assertNotIn("Lina Haddad", log)
        self.assertNotIn("0599000111", log)
        self.assertNotIn("slot_id=", log)


if __name__ == "__main__":
    unittest.main()
