"""Smoke test for the kit. Start the simulated clinic first:

    uvicorn services.clinic_api:app --port 8101
    python check_kit.py

It checks the API, the API key, the MCP server over stdio and one read call,
then the optional voice pieces (warnings only). Ends with "Kit ready." when the
required checks pass.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPECTED_TOOLS = {"clinic_info", "list_services", "find_slots", "get_patient", "save_patient",
                  "create_booking", "get_booking", "cancel_booking"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

API_URL = (os.getenv("CLINIC_API_URL") or "http://localhost:8101").rstrip("/")
API_KEY = os.getenv("CLINIC_API_KEY") or "dev-key-change-me"
failures: list[str] = []


def ok(msg: str) -> None:
    print(f"✅ {msg}")


def warn(msg: str) -> None:
    print(f"⚠️  {msg}")


def fail(msg: str) -> None:
    print(f"❌ {msg}")
    failures.append(msg)


def check_api() -> dict | None:
    import httpx

    try:
        r = httpx.get(f"{API_URL}/health", timeout=5)
        r.raise_for_status()
        ok(f"Clinic API is up at {API_URL} ({r.json().get('service')})")
    except Exception as exc:  # noqa: BLE001
        fail(f"Clinic API not answering at {API_URL}/health ({exc.__class__.__name__}). "
             "Start it: uvicorn services.clinic_api:app --port 8101")
        return None
    r = httpx.get(f"{API_URL}/api/clinic", headers={"X-API-Key": API_KEY}, timeout=5)
    if r.status_code == 401:
        fail("The API refused the key (401). CLINIC_API_KEY must be the same for the API and for this shell.")
        return None
    info = r.json()
    ok(f"API key accepted. Clinic today: {info['weekday']} {info['today']} ({info['timezone']})")
    return info


async def check_mcp(today: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "services.clinic_mcp"],
                                   cwd=str(ROOT), env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            missing = EXPECTED_TOOLS - tools
            if missing:
                fail(f"MCP server is missing tools: {sorted(missing)}")
            else:
                ok(f"MCP server (stdio) lists {len(tools)} tools: {', '.join(sorted(tools))}")
            res = await session.call_tool("find_slots", {"service_id": "dental_checkup", "date_from": today,
                                                         "date_to": today, "limit": 3})
            data = res.structuredContent or {}
            if data.get("ok"):
                times = ", ".join(f"{s['start']} {s['provider_name']}" for s in data["slots"]) or "none free today"
                ok(f"MCP find_slots works: dental check-up today -> {times}")
            else:
                fail(f"MCP find_slots failed: {data.get('error') or res.content}")


def check_voice() -> None:
    sys.path.insert(0, str(ROOT))
    stt = os.getenv("STT_PROVIDER") or "openrouter"
    tts = os.getenv("TTS_PROVIDER") or "piper"
    if stt == "openrouter":
        if os.getenv("OPENROUTER_API_KEY"):
            ok(f"Speech-to-text: openrouter ({os.getenv('STT_MODEL') or 'google/gemini-2.5-flash-lite'}), key found")
        else:
            warn("Speech-to-text: OPENROUTER_API_KEY is empty (copy .env.example to .env and fill it in)")
    elif importlib.util.find_spec("faster_whisper"):
        ok("Speech-to-text: whisper_local (faster-whisper installed)")
    else:
        warn("Speech-to-text: STT_PROVIDER=whisper_local but faster-whisper is not installed")
    if tts == "piper":
        if importlib.util.find_spec("piper"):
            from voice.tts import voice_status

            missing = [v for v, present in voice_status().items() if not present]
            if missing:
                warn(f"Text-to-speech: piper installed; voices download on first use ({', '.join(missing)}): "
                     "python -m voice.tts --download")
            else:
                ok("Text-to-speech: piper installed, English and Arabic voices present")
        else:
            warn("Text-to-speech: piper-tts is not installed (pip install -r requirements.txt)")
    else:
        ok(f"Text-to-speech: {tts} (the browser speaks the reply)")


def main() -> int:
    print(f"Checking the kit in {ROOT}\n")
    info = check_api()
    if info:
        try:
            asyncio.run(check_mcp(info["today"]))
        except Exception as exc:  # noqa: BLE001
            fail(f"Could not run the MCP server over stdio: {exc!r}")
    check_voice()
    print()
    if failures:
        print(f"❌ Kit not ready: {len(failures)} problem(s) above.")
        return 1
    print("✅ Kit ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
