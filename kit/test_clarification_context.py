# اختبارات مركزة لتعارض التاريخ واستكمال فترة الساعة دون فقد قائمة أو بيانات المريض.
import unittest
from agent.dates import normalize_date
from agent.slot_selection import select_slot
import test_arabic_conversation as conversation


class ClarificationContextTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = conversation.ArabicConversationTests.asyncSetUp
    asyncTearDown = conversation.ArabicConversationTests.asyncTearDown
    send = conversation.ArabicConversationTests.send
    state = conversation.ArabicConversationTests.state
    search = conversation.ArabicConversationTests.search

    async def test_arabic_date_correction_after_past_date(self):
        from unittest.mock import patch

        read = self.gateway.call_read

        async def clinic_read(tool, args):
            result = await read(tool, args)
            if tool == "clinic_info":
                result.update(today="2026-11-11", now="2026-11-11T08:00:00+02:00")
            return result

        with patch.object(self.gateway, "call_read", side_effect=clinic_read):
            initial = await self.send("بدي فحص أسنان", service_text="فحص أسنان", provider_any=True)
            self.assertEqual(initial["reply"], "في أي يوم ترغب بحجز الموعد؟")
            for future in ("الثانية عشر من نوفمبر", "١٢ نوفمبر", "12 نوفمبر", "الثاني عشر من نوفمبر",
                           "اثنا عشر نوفمبر", "يوم الخميس", "بكرا"):
                with self.subTest(future=future):
                    self.gateway.reads.clear()
                    past = await self.send("١٠ نوفمبر")
                    with self.subTest(turn="past"):
                        self.assertIn("مرق", past["reply"])
                        self.assertIsNone(self.state()["requested_date"])
                        self.assertIsNone(past.get("pending_action"))
                        self.assertFalse(any(tool == "find_slots" for tool, _ in self.gateway.reads))
                    corrected = await self.send(future)
                    self.assertEqual(self.state()["service_id"], "dental_checkup")
                    self.assertEqual(self.state()["requested_date"], "2026-11-12")
                    self.assertEqual(corrected["state"], "slots_proposed")

    async def test_period_reply_completes_existing_hour(self):
        for first, period, expected in (("at 3", "afternoon", "15:00"), ("at 9", "morning", "09:00"),
                                        ("الساعة 3", "مساء", "15:00"), ("الساعة 9", "صباح", "09:00")):
            await self.send("dental checkup", service_text="dental checkup", date_text="November 2, 2026", time_text=first, provider_any=True)
            self.assertEqual(self.state()["time_kind"], "ambiguous")
            result = await self.send(period, time_text=period)
            self.assertEqual(self.state()["requested_time"], expected)
            self.assertNotEqual(result["state"], "awaiting_time_clarification")

    async def test_full_weekday_conflict_asks_for_the_intended_date(self):
        self.assertIsNone(normalize_date("Thursday, November 3, 2026", "2026-11-02"))
        data = await self.send("Thursday, November 3, 2026", language="en", service_text="dental checkup", date_text="Thursday, November 3, 2026")
        self.assertIn("conflict", data["reply"].lower())
        self.assertIsNone(self.state()["requested_date"])
        self.assertIsNone(self.state()["pending_action"])

    async def test_period_and_doctor_clarification_preserves_current_list(self):
        await self.search()
        original = self.state()["proposed_slots"]
        await self.send("3:45", time_text="3:45")
        await self.send("afternoon", language="en", time_text="afternoon")
        self.assertEqual(self.state()["proposed_slots"], original)
        self.assertEqual(self.state()["selection_candidates"], ["SL-CONV-3", "SL-CONV-4"])
        data = await self.send("Dr. Sami", language="en", provider_text="Dr. Sami")
        self.assertEqual(data["state"], "awaiting_patient_details")
        self.assertEqual(self.state()["selected_slot_id"], "SL-CONV-4")

    async def test_full_date_conflict_keeps_the_existing_offer(self):
        await self.search()
        original = self.state()["proposed_slots"]
        await self.send("Thursday, November 3, 2026", language="en", date_text="Thursday, November 3, 2026")
        self.assertEqual(self.state()["proposed_slots"], original)
        self.assertEqual(self.state()["requested_date"], "2026-11-02")
        self.assertIsNone(self.state()["pending_action"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
