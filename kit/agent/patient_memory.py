
# ==================================================
# تذكير — patient_memory.py
#
# الهدف: استرجاع بيانات المرضى العائدين.
#
# 1. نستقبل رقم هاتف المريض.
# 2. نستدعي get_patient عبر ClinicGateway.
# 3. إذا وجدناه نعيد معلوماته الأساسية.
# 4. إذا لم نجده نعامله كمريض جديد.
# 5. إذا فشلت الأداة نميز العطل عن المريض الجديد.
#
# هذا الملف للقراءة فقط ولا ينشئ حجوزات.
# ==================================================

from core.contracts import ClinicGateway


async def lookup_returning_patient(
    gateway: ClinicGateway,
    phone: str
) -> dict:

    # لا نستدعي العيادة إذا لم يصل رقم هاتف.
    if not phone or not phone.strip():
        return {"status": "missing"}

    # استدعاء أداة قراءة بيانات المريض.
    try:
        response = await gateway.call_read(
            "get_patient",
            {"phone": phone.strip()}
        )
    except Exception:
        return {"status": "unavailable"}

    # التأكد من أن الأداة أعادت بيانات صحيحة الشكل.
    if not isinstance(response, dict):
        return {"status": "unavailable"}

    # الحالة الأولى: المريض موجود.
    if response.get("ok") is True:
        return {
            "status": "found",
            "name": response.get("name"),
            "preferred_provider_id": response.get(
                "preferred_provider_id"
            ),
            "preferred_language": response.get("language"),
        }

    # قراءة رمز الخطأ الذي أعادته العيادة.
    error = response.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None

    # الحالة الثانية: رقم الهاتف غير مسجل.
    if error_code == "unknown_patient":
        return {"status": "new"}

    # الحالة الثالثة: فشل الاتصال أو حدث خطأ آخر.
    return {"status": "unavailable"}
