# يختبر انتقال الوقت من Intake عبر Graph إلى Scheduler ثم تصفية النتائج.
# المدخل حالات ورسائل وهمية؛ المخرج assertions ونتائج مطبوعة.
# يستبدل LLM وGateway ببدائل محلية؛ لا يستخدم MCP أو API ولا ينشئ حجزًا.
import asyncio
from unittest.mock import patch

from agent.graph import graph
from agent.intake import IntakeResult
from agent.state import create_initial_state


# بوابة وهمية تسجل قراءات البحث وتعيد عمدًا مواعيد تخالف الوقت المطلوب.
# الهدف اختبار معاملات البحث والتحقق الدفاعي من النتائج بصورة مستقلة.
class FakeGateway:
    def __init__(self):
        self.searches = []

    async def call_read(self, name, args):
        if name == "clinic_info":
            return {"ok": True, "today": "2026-11-02"}
        if name == "find_slots":
            self.searches.append(args)
            return {"ok": True, "slots": [
                {"slot_id": f"TEST-{start}", "date": "2026-11-03",
                 "start": start, "service_id": "dental_checkup",
                 "provider_id": "dr_khoury", "provider_name": "Dr. Rania Khoury"}
                for start in ("09:00", "15:00", "15:15", "16:30", "17:00")
            ] + [{"slot_id": "TEST-WRONG-DATE", "date": "2026-11-04", "start": "16:30",
                  "service_id": "dental_checkup", "provider_id": "dr_khoury",
                  "provider_name": "Dr. Rania Khoury"}]}
        raise AssertionError(f"Unexpected tool: {name}")

    async def call_write(self, name, args):
        raise AssertionError("No writes are allowed in this test")


# تشغّل Graph باستخراج ثابت وتعيد الحالة والبوابة لفحص المدخلات والمخرجات.
# تهيئ خدمة موثقة لتبقى الاختبارات مركزة على انتقال الوقت بين العقد.
async def search(time_text):
    state = create_initial_state("graph-time-test")
    state.update({"language": "en", "service_text": "dental checkup",
                  "service_id": "dental_checkup", "provider_choice": "any"})
    state["current_message"] = f"Dental checkup on November 3, 2026 {time_text or ''}"
    extracted = IntakeResult(language="en", intent="booking",
                             date_text="November 3, 2026", time_text=time_text)

    async def fake_extract(message):
        return extracted

    gateway = FakeGateway()
    with patch("agent.graph.extract_request", new=fake_extract):
        updated = await graph.ainvoke(state, context={"gateway": gateway})
    return updated, gateway


# تتحقق من القيود الإنجليزية والعربية والساعة المحددة والغموض وغياب التفضيل.
# فحص after/before يثبت صحة الطلب؛ فحص المقترحات يثبت رفض نتائج الأداة المخالفة.
async def main():
    for text in ("after 3 PM", "بعد 3 العصر"):
        state, gateway = await search(text)
        assert state["stage"] == "slots_proposed"
        assert state["time_kind"] == "window"
        assert state["time_after"] == "15:01"
        assert state["time_before"] is None
        assert len(gateway.searches) == 1
        assert gateway.searches[0]["after"] == "15:01"
        assert [slot["start"] for slot in state["proposed_slots"]] == ["15:15", "16:30", "17:00"]
        assert all(slot["date"] == "2026-11-03" for slot in state["proposed_slots"])
        assert "TEST-WRONG-DATE" not in state["reply"]
    print("PASSED: English and Arabic after-3 requests exclude morning, 15:00, and wrong dates")

    state, gateway = await search("4:30 PM")
    assert state["time_kind"] == "exact"
    assert gateway.searches[0]["after"] == "16:30"
    assert gateway.searches[0]["before"] == "16:31"
    assert [slot["start"] for slot in state["proposed_slots"]] == ["16:30"]
    print("PASSED: exact time reaches the tool and filters other slots")

    for text in ("4:30", "some unusual time"):
        state, gateway = await search(text)
        assert state["stage"] == "awaiting_time_clarification"
        assert gateway.searches == []
        assert state["proposed_slots"] == []
    print("PASSED: ambiguous and unrecognized times do not trigger a slot search")

    state, gateway = await search(None)
    assert state["time_kind"] == "missing"
    assert "after" not in gateway.searches[0]
    assert "before" not in gateway.searches[0]
    assert len(state["proposed_slots"]) == 5
    print("PASSED: missing time leaves the search unrestricted")
    print("ALL GRAPH TIME FILTER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
