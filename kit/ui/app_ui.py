"""Gradio client for the receptionist API contract: POST /api/chat, /api/voice and /api/approve.

Start your app first (APP_URL, default http://localhost:8000), then:
    python ui/app_ui.py        ->  http://localhost:7860

Speak (record or upload) or type. When your app proposes a booking (pending_action), Approve or
Reject it. Replies are spoken: the app's audio_b64 when present, otherwise the browser's own voice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import gradio as gr
import httpx

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

APP_URL = (os.getenv("APP_URL") or "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.getenv("UI_TIMEOUT") or 120)

# Runs in the browser after every reply: play the app's audio, or speak the text with speechSynthesis.
SPEAK_JS = """(speech) => {
  if (!speech) return;
  const s = JSON.parse(speech);
  window.speechSynthesis && speechSynthesis.cancel();
  if (s.audio) { new Audio(s.audio).play().catch((e) => console.warn("autoplay blocked", e)); return; }
  if (!s.text || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(s.text);
  u.lang = /[\\u0600-\\u06FF]/.test(s.text) ? "ar-SA" : "en-US";
  const v = speechSynthesis.getVoices().find((x) => x.lang.startsWith(u.lang.slice(0, 2)));
  if (v) u.voice = v;
  speechSynthesis.speak(u);
}"""


def post(path: str, **kwargs) -> dict:
    try:
        r = httpx.post(APP_URL + path, timeout=TIMEOUT, **kwargs)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"reply": f"Cannot reach your app at {APP_URL}{path} ({exc.__class__.__name__})."}
    if r.status_code >= 400 or not isinstance(data, dict):
        return {"reply": f"Your app answered HTTP {r.status_code}: {json.dumps(data)[:300]}"}
    return data


def session_id(request: gr.Request) -> str:
    return "ui-" + (request.session_hash or "local")


def show(data: dict, history: list) -> tuple:
    """Map one ChatResponse onto the UI components."""
    history = history + [{"role": "assistant", "content": data.get("reply") or "(empty reply)"}]
    pending = data.get("pending_action")
    calls = data.get("tool_calls") or []
    tools_md = "\n".join(f"- `{c.get('tool')}` {'ok' if c.get('ok') else '**failed**'} ({c.get('ms', '?')} ms)"
                         for c in calls) or "_none_"
    status = f"**State:** {data.get('state') or '-'}"
    if data.get("result"):
        status += f"  \n**Result:** `{json.dumps(data['result'], ensure_ascii=False)}`"
    if (data.get("usage") or {}).get("cost_usd") is not None:
        status += f"  \n**Cost of this turn:** ${float(data['usage']['cost_usd']):.4f}"
    pending_md = f"### Approve this?\n{pending.get('summary', '')}" if pending else ""
    audio = f"data:{data.get('audio_mime') or 'audio/wav'};base64,{data['audio_b64']}" if data.get("audio_b64") else None
    speech = json.dumps({"text": data.get("reply") or "", "audio": audio})
    return (history, data.get("transcript") or "", status, tools_md, pending_md, pending,
            gr.update(visible=bool(pending)), gr.update(visible=bool(pending)), speech)


def send_text(message: str, history: list, request: gr.Request):
    if not (message or "").strip():
        raise gr.Error("Type a message first.")
    history = history + [{"role": "user", "content": message}]
    data = post("/api/chat", json={"session_id": session_id(request), "message": message})
    return show(data, history) + ("",)


def send_voice(audio_path: str | None, history: list, request: gr.Request):
    if not audio_path:
        raise gr.Error("Record or upload a recording first.")
    with open(audio_path, "rb") as fh:
        data = post("/api/voice", data={"session_id": session_id(request)},
                    files={"audio": (Path(audio_path).name, fh, "audio/wav")})
    heard = data.get("transcript") or "(nothing heard)"
    return show(data, history + [{"role": "user", "content": f"(voice) {heard}"}])


def decide(pending: dict | None, history: list, request: gr.Request, approved: bool):
    if not pending:
        raise gr.Error("Nothing is waiting for approval.")
    history = history + [{"role": "user", "content": ("Approved: " if approved else "Rejected: ") + pending.get("summary", "")}]
    data = post("/api/approve", json={"session_id": session_id(request), "action_id": pending.get("action_id"),
                                      "approved": approved})
    return show(data, history)


def approve(pending, history, request: gr.Request):
    return decide(pending, history, request, True)


def reject(pending, history, request: gr.Request):
    return decide(pending, history, request, False)


with gr.Blocks(title="Clinic receptionist") as demo:
    gr.Markdown(f"## Clinic receptionist\nConnected to `{APP_URL}`. Speak or type in English or Arabic.")
    pending_state = gr.State(None)
    speech = gr.Textbox(visible=False)
    with gr.Row():
        with gr.Column(scale=3):
            chat = gr.Chatbot(label="Conversation", height=430)
            with gr.Row():
                msg = gr.Textbox(placeholder="Type a message and press Enter", show_label=False, scale=5)
                send = gr.Button("Send", variant="primary", scale=1)
            mic = gr.Audio(sources=["microphone", "upload"], type="filepath",
                           label="Speak: record (sent when you stop) or upload a WAV/MP3 file")
        with gr.Column(scale=2):
            transcript = gr.Textbox(label="Transcript (what your app heard)", interactive=False)
            status = gr.Markdown("**State:** -")
            pending_md = gr.Markdown()
            with gr.Row():
                approve_btn = gr.Button("Approve", variant="primary", visible=False)
                reject_btn = gr.Button("Reject", variant="stop", visible=False)
            gr.Markdown("**Tool calls in this turn**")
            tools = gr.Markdown("_none_")

    outs = [chat, transcript, status, tools, pending_md, pending_state, approve_btn, reject_btn, speech]
    for event in (msg.submit, send.click):
        event(send_text, [msg, chat], outs + [msg]).then(None, [speech], None, js=SPEAK_JS)
    for event in (mic.stop_recording, mic.upload):
        event(send_voice, [mic, chat], outs).then(None, [speech], None, js=SPEAK_JS)
    approve_btn.click(approve, [pending_state, chat], outs).then(None, [speech], None, js=SPEAK_JS)
    reject_btn.click(reject, [pending_state, chat], outs).then(None, [speech], None, js=SPEAK_JS)

if __name__ == "__main__":
    demo.launch(server_port=int(os.getenv("UI_PORT") or 7860))
