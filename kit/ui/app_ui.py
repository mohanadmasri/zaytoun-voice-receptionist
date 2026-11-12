# واجهة Gradio الموجودة لعرض المحادثة والتسجيل والنص والخيارات وحالة الموافقة.
# المدخل نص أو تسجيل ميكروفون WAV؛ المخرج رد API مع transcript ونص الرد.
# لا تتصل مباشرة بـLLM أو MCP؛ ترسل إلى FastAPI، وتنفذ الموافقة بزر مستقل فقط.
"""Gradio client for the receptionist API contract: POST /api/chat, /api/voice and /api/approve.

Start your app first (APP_URL, default http://localhost:8000), then:
    python ui/app_ui.py        ->  http://localhost:7860

Speak (record or upload) or type. When your app proposes a booking (pending_action), Approve or
Reject it. Spoken replies are available in the main clinic interface.
"""
from __future__ import annotations

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


# ترسل الطلب إلى FastAPI وتحتفظ برد الأخطاء الصوتية المنظم والموافقة الموجودة فيه.
# أخطاء النقل تبقى رسائل واضحة؛ لا تعيد إرسال الطلب تلقائيًا لتجنب تكرار المعالجة.
def post(path: str, **kwargs) -> dict:
    try:
        r = httpx.post(APP_URL + path, timeout=TIMEOUT, **kwargs)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"reply": "Cannot reach the receptionist. Please try again. / تعذر الاتصال بالمساعد. جرّب مرة تانية."}
    if isinstance(data, dict) and isinstance(data.get("reply"), str):
        return data
    if r.status_code >= 400 or not isinstance(data, dict):
        return {"reply": "We could not complete that request. Please retry. / تعذر إكمال الطلب. جرّب مرة تانية."}
    return data


# تربط جميع رسائل النص والصوت والموافقة بنفس جلسة المتصفح بدل إنشاء جلسة لكل تسجيل.
def session_id(request: gr.Request) -> str:
    if not request.session_hash:
        raise gr.Error("Please reload the page to start a conversation / أعد تحميل الصفحة لبدء جلسة.")
    return "ui-" + request.session_hash


# تحول رد العقد إلى حقول الواجهة، وتحافظ على الخيارات المرقمة داخل reply كما وردت.
def show(data: dict, history: list) -> tuple:
    """Map one ChatResponse onto the UI components."""
    history = history + [{"role": "assistant", "content": data.get("reply") or "(empty reply)"}]
    pending = data.get("pending_action")
    pending_heading = "بتوافق على هالموعد؟" if data.get("language") == "ar" else "Approve this?"
    pending_md = f"### {pending_heading}\n{pending.get('summary', '')}" if pending else ""
    return (history, data.get("transcript") or "", pending_md, pending,
            gr.update(visible=bool(pending)), gr.update(visible=bool(pending)))


# ترسل نصًا عبر /api/chat بنفس جلسة الميكروفون، ليبقى الإدخال الكتابي بديلًا دائمًا.
def send_text(message: str, history: list, request: gr.Request):
    if not (message or "").strip():
        raise gr.Error("Type a message first.")
    continuation = bool(history)
    history = history + [{"role": "user", "content": message}]
    data = post("/api/chat", json={"session_id": session_id(request), "message": message, "continuation": continuation})
    return show(data, history) + ("",)


# ترفع التسجيل إلى /api/voice؛ لا تفرغه محليًا ولا تفقد session_id بين الأدوار.
def send_voice(audio_path: str | None, history: list, request: gr.Request, language: str = "العربية"):
    if not audio_path:
        raise gr.Error("Record or upload a recording first.")
    with open(audio_path, "rb") as fh:
        data = post("/api/voice", data={"session_id": session_id(request), "language_hint": "en" if language == "English" else "ar",
                                       "continuation": str(bool(history)).lower()},
                    files={"audio": (Path(audio_path).name, fh, "audio/wav")})
    if heard := data.get("transcript"):
        history = history + [{"role": "user", "content": f"(voice preview) {heard}"}]
    return show(data, history) + (data.get("transcript_token"),)


# تأكيد النص المراجع فقط؛ لا علاقة له بزر موافقة الحجز، ولا يعيد رفع الملف الصوتي.
def use_transcript(token, history, request: gr.Request):
    if not token:
        raise gr.Error("Record again and review the transcript / أعد التسجيل وراجع النص أولًا.")
    data = post("/api/voice/confirm", json={"session_id": session_id(request), "transcript_token": token})
    return show(data, history) + (None,)


# ترسل قرار الزر ومعرف العملية فقط؛ تفاصيل الحجز تُقرأ من الخادم لا من المتصفح.
def decide(pending: dict | None, history: list, request: gr.Request, approved: bool):
    if not pending:
        raise gr.Error("Nothing is waiting for approval.")
    history = history + [{"role": "user", "content": ("Approved: " if approved else "Rejected: ") + pending.get("summary", "")}]
    data = post("/api/approve", json={"session_id": session_id(request), "action_id": pending.get("action_id"),
                                      "approved": approved})
    return show(data, history)


# يمرر زر الموافقة الصريحة؛ تسجيل كلمة yes وحده لا يستدعي هذه الدالة.
def approve(pending, history, request: gr.Request):
    return decide(pending, history, request, True)


# يمرر الرفض لنفس العملية المعلقة دون تنفيذ حجز.
def reject(pending, history, request: gr.Request):
    return decide(pending, history, request, False)


# الألوان والتخطيط محليان؛ لا خطوط أو صور خارجية ولا بيانات مرضى داخل CSS.
UI_CSS = """
.gradio-container {max-width:1200px!important; margin:auto!important; font-family:Segoe UI,Tahoma,sans-serif!important; background:#f5f8f7; color:#183b34}
#clinic-header {padding:28px 30px; border-radius:24px; background:linear-gradient(120deg,#123c35,#216457); color:white; margin:12px 0 20px}
#clinic-header h1 {font-size:clamp(23px,3vw,34px); margin:6px 0; color:white}
#clinic-header span {color:#d3e6dd!important; font-size:13px; letter-spacing:2px}
#clinic-header p {color:#d3e6dd; margin:6px 0}
.clinic-card {background:white!important; border:1px solid #dce8e2!important; border-radius:20px!important; padding:20px!important; box-shadow:0 6px 24px #143c3508}
#conversation {border-radius:16px; border-color:#dfebe5}
#conversation .message {unicode-bidi:plaintext; text-align:start; line-height:1.8}
#conversation .message p, #approval-card {unicode-bidi:plaintext; white-space:pre-wrap; line-height:1.8}
#conversation .message p:dir(rtl) {text-align:right}
#conversation .message p:dir(ltr) {text-align:left}
#approval-card {background:#eff7f2; border:1px solid #bdd7c7; border-radius:16px; padding:16px}
#connection {font-size:13px; color:#376657; padding:4px 8px}
#privacy-note {color:#64796f; font-size:13px}
button {border-radius:12px!important; min-height:44px!important}
@media(max-width:700px) {#clinic-header {padding:20px} .clinic-card {padding:12px!important} #conversation {height:380px!important}}
"""


# فحص اتصال المساعد فقط؛ لا يدعي جاهزية العيادة أو خدمة التعرف على الصوت.
def connection_status():
    try:
        response = httpx.get(APP_URL + "/health", timeout=3)
        if response.is_success and response.json().get("status") == "ok":
            return "● Receptionist connected · المساعد متصل"
    except Exception:
        pass
    return "○ Receptionist unavailable · المساعد غير متصل — أعد المحاولة"


# يبطل نتيجة تسجيل قديم عند بدء تسجيل جديد، دون مسح أي بيانات حجز.
def start_recording(request: gr.Request):
    post("/api/voice/discard", json={"session_id": session_id(request)})
    return None


with gr.Blocks(title="Zaytoun Family Clinic — AI Voice Receptionist") as demo:
    gr.HTML('<header id="clinic-header"><span>ZA Y T O U N · عيادة الزيتون</span><h1>Zaytoun Family Clinic — AI Voice Receptionist</h1><p>Appointments made simple · مواعيدك بكل سهولة</p></header>')
    with gr.Row():
        connection = gr.Markdown("Checking connection / جارٍ التحقق…", elem_id="connection", scale=5)
        refresh = gr.Button("Refresh connection / تحديث الاتصال", size="sm", scale=1)
    pending_state = gr.State(None)
    voice_token = gr.State(None)
    with gr.Row(equal_height=False):
        with gr.Column(scale=7, min_width=300, elem_classes="clinic-card"):
            gr.Markdown("### Your conversation · محادثتك")
            chat = gr.Chatbot(label="Conversation / المحادثة", height=440, elem_id="conversation")
            with gr.Row():
                msg = gr.Textbox(placeholder="اكتب رسالتك هنا · Type your message", show_label=False, scale=5)
                send = gr.Button("Send / إرسال", variant="primary", scale=1)
            gr.Markdown("Record, then stop — we will listen and reply. / سجّل وأوقف التسجيل؛ المساعد بيسمع وبيرد.")
            recording_language = gr.Radio(["العربية", "English"], value="العربية", label="Recording language / لغة التسجيل")
            mic = gr.Audio(sources=["microphone", "upload"], type="filepath", format="wav", label="Microphone / الميكروفون")
            gr.Markdown("You can correct anything we heard by typing. / صحح أي تفريغ بالكتابة.", elem_id="privacy-note")
        with gr.Column(scale=4, min_width=280, elem_classes="clinic-card"):
            gr.Markdown("### Review your appointment · راجع موعدك")
            pending_md = gr.Markdown("Your appointment summary appears here before any booking. / ملخص الموعد بيظهر هون قبل أي حجز.", elem_id="approval-card")
            with gr.Row():
                approve_btn = gr.Button("Approve appointment / تأكيد الموعد", variant="primary", visible=False)
                reject_btn = gr.Button("Reject / رفض", variant="stop", visible=False)
            gr.Markdown("**Nothing is booked until you approve the exact details.** / **ما بنحجز قبل موافقتك على التفاصيل.**")
            transcript = gr.Textbox(label="Transcript / النص المسموع", interactive=False)
    outs = [chat, transcript, pending_md, pending_state, approve_btn, reject_btn]
    demo.load(connection_status, None, connection)
    refresh.click(connection_status, None, connection)
    for event in (msg.submit, send.click):
        event(send_text, [msg, chat], outs + [msg], concurrency_id="conversation", concurrency_limit=1)
    for event in (mic.stop_recording, mic.upload):
        event(send_voice, [mic, chat, recording_language], outs + [voice_token], concurrency_id="conversation", concurrency_limit=1)
    mic.start_recording(start_recording, None, voice_token, queue=False)
    mic.clear(start_recording, None, voice_token, queue=False)
    approve_btn.click(approve, [pending_state, chat], outs, concurrency_id="conversation", concurrency_limit=1)
    reject_btn.click(reject, [pending_state, chat], outs, concurrency_id="conversation", concurrency_limit=1)

if __name__ == "__main__":
    demo.launch(server_port=int(os.getenv("UI_PORT") or 7860), css=UI_CSS,
                theme=gr.themes.Soft(primary_hue="teal", neutral_hue="slate"), footer_links=[])
