# دعم الاختبارات القديمة: ينفذ دور تأكيد الهاتف الصريح الجديد، ولا يوافق على أي حجز.
from unittest.mock import AsyncMock, patch
import httpx
from agent.intake import IntakeResult


async def confirm_phone_turn(client, response, session_id):
    data = response.json()
    if response.status_code != 200 or data.get("state") != "awaiting_phone_confirmation":
        return response
    assert data.get("pending_action") is None, "Booking approval must not exist before phone confirmation"
    language = data.get("language") if data.get("language") in ("ar", "en") else "en"
    with patch("agent.graph.extract_request", new=AsyncMock(return_value=IntakeResult(language=language, intent="other"))):
        confirmed = await client.post("/api/chat", json={"session_id": session_id, "message": "نعم" if language == "ar" else "Yes, that phone number is correct"})
    assert confirmed.status_code == 200, confirmed.text
    return httpx.Response(confirmed.status_code, json={**confirmed.json(), **{key: data[key] for key in ("transcript", "audio_b64", "audio_mime") if key in data}})
