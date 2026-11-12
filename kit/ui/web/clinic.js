import { createRecorder } from "./voice.js";
import { createSpeechStream } from "./speech.js";
import { renderAppointments } from "./appointments.js";

const labels = {
  en: {
    brand: "Zaytoun Family Clinic", subtitle: "Digital reception", title: "Appointment assistant",
    appointments: "My Appointments", review: "Review details", greeting: "How can we help you?",
    intro: "Ask about clinic services, find a time, or review an appointment. You can type or use the microphone.",
    approvalNote: "You’ll review the details before confirming a booking or cancellation.",
    clinicInfo: "About the clinic", clinicBody: "Bilingual reception for the simulated Zaytoun Family Clinic.",
    conversation: "Conversation", message: "Message the receptionist…",
    send: "Send message", mic: "Start recording", stop: "Stop and send recording", cancel: "Delete recording",
    requesting: "Opening microphone…", recording: "Recording", stopping: "Preparing audio…",
    listening: "Processing your recording…", working: "The receptionist is responding…",
    checking: "Connecting", online: "Connected", offline: "Disconnected",
    you: "You", voice: "Voice transcript", assistant: "Zaytoun reception", replay: "Play reply",
    spokenReplies: "Spoken replies", speechBlocked: "Select Play reply to try again.",
    speechExpired: "This reply’s audio has expired.",
    speechInterrupted: "Audio was interrupted. Select Play reply to hear it again.",
    permission: "Microphone access was denied. Allow it in your browser or type your message.",
    unsupported: "The microphone is unavailable in this browser. You can type your message.",
    recordingError: "We couldn’t capture your recording. Try again or type your message.",
    tooLong: "That recording was too long. Try a shorter recording.",
    phoneTranscription: "We couldn’t recognize the phone number. Say each digit separately or type the number.",
    sttRetry: "Speech recognition is temporarily unavailable. Record again or type your message.",
    transcription: "We couldn’t transcribe that recording. Try again in the selected language or type your message.",
    requestError: "We couldn’t complete your request. Please try again.",
    timeout: "The response took too long. Please try again.",
    reset: "The previous conversation expired. Review any appointment details again before approving.",
  },
  ar: {
    brand: "عيادة الزيتون للعائلة", subtitle: "الاستقبال الرقمي", title: "مساعد المواعيد",
    appointments: "مواعيدي", review: "راجع التفاصيل", greeting: "كيف فينا نساعدك؟",
    intro: "اسأل عن خدمات العيادة، أو ابحث عن موعد، أو راجع موعدك. فيك تكتب أو تستخدم الميكروفون.",
    approvalNote: "بتراجع تفاصيل الموعد قبل تأكيد الحجز أو الإلغاء.",
    clinicInfo: "عن العيادة", clinicBody: "استقبال باللغتين العربية والإنجليزية لعيادة الزيتون العائلية التجريبية.",
    conversation: "المحادثة", message: "اكتب رسالتك للاستقبال…",
    send: "إرسال الرسالة", mic: "بدء التسجيل", stop: "إيقاف التسجيل وإرساله", cancel: "حذف التسجيل",
    requesting: "جارٍ تشغيل الميكروفون…", recording: "جارٍ التسجيل", stopping: "جارٍ تجهيز التسجيل…",
    listening: "جارٍ معالجة التسجيل…", working: "جارٍ تحضير رد الاستقبال…",
    checking: "جارٍ الاتصال", online: "متصل", offline: "غير متصل",
    you: "أنت", voice: "النص المسموع", assistant: "استقبال الزيتون", replay: "تشغيل الرد",
    spokenReplies: "الردود الصوتية", speechBlocked: "اضغط تشغيل الرد للمحاولة مرة ثانية.",
    speechExpired: "انتهت صلاحية صوت هالرد.",
    speechInterrupted: "انقطع الصوت. اضغط تشغيل الرد لتسمعه مرة ثانية.",
    permission: "ما قدرنا نوصل للميكروفون. اسمح بالوصول من المتصفح أو اكتب رسالتك.",
    unsupported: "الميكروفون غير متاح في هذا المتصفح. فيك تكتب رسالتك.",
    recordingError: "تعذّر تسجيل الصوت. جرّب مرة ثانية أو اكتب رسالتك.",
    tooLong: "التسجيل طويل. جرّب تسجيل أقصر.",
    phoneTranscription: "ما قدرنا نميّز رقم التلفون. احكي الأرقام رقم رقم أو اكتب الرقم.",
    sttRetry: "تعذّر التعرّف على الصوت مؤقتًا. ممكن إعادة التسجيل أو كتابة الرسالة.",
    transcription: "تعذّر تفريغ التسجيل. جرّب مرة ثانية باللغة المختارة أو اكتب رسالتك.",
    requestError: "تعذّر إكمال الطلب. جرّب مرة ثانية.",
    timeout: "الرد أخذ وقت طويل. جرّب مرة ثانية.",
    reset: "انتهت الجلسة السابقة. راجع تفاصيل الموعد مرة ثانية قبل الموافقة.",
  },
};

const byId = (id) => document.getElementById(id);
const message = byId("message");
const mic = byId("mic");
const send = byId("send");
const cancel = byId("cancel-recording");
const appointments = byId("appointments");
const history = byId("messages");
const session = Array.from(crypto.getRandomValues(new Uint8Array(24)),
  (byte) => byte.toString(16).padStart(2, "0")).join("");
let records = [];
let language = "en";
let selectedLanguage = null;
try { selectedLanguage = localStorage.getItem("zaytoun.language"); } catch {}
let pending = null;
let busy = null;
let recording = "idle";
let seconds = 0;
let connection = "checking";
let error = null;
let continuation = false;
const speechStream = createSpeechStream();
let playbackGeneration = 0;
let speechIssue = null;
let muted = false;
try { muted = localStorage.getItem("zaytoun.muted") === "true"; } catch {}

function accessibleLabel(control, label) {
  control.setAttribute("aria-label", label);
  control.title = label;
}

function updateControls() {
  const t = labels[language];
  byId("spoken-replies").setAttribute("aria-pressed", String(!muted));
  byId("speech-status").hidden = !speechIssue;
  byId("speech-status").textContent = speechIssue ? t[speechIssue] : "";
  const capturing = recording !== "idle";
  const showingStop = recording === "recording" || recording === "stopping";
  message.hidden = capturing;
  message.readOnly = Boolean(busy);
  send.hidden = capturing;
  send.disabled = Boolean(busy) || !message.value.trim();
  mic.disabled = Boolean(busy) || recording === "requesting" || recording === "stopping";
  mic.dataset.recording = String(showingStop);
  mic.querySelector("use").setAttribute("href", showingStop ? "#stop" : "#microphone");
  accessibleLabel(mic, showingStop ? t.stop : t.mic);
  accessibleLabel(send, t.send);
  accessibleLabel(cancel, t.cancel);
  cancel.hidden = recording !== "recording";
  byId("recording").hidden = !capturing;
  if (capturing && byId("record-label").textContent !== t[recording]) {
    byId("record-label").textContent = t[recording];
  }
  const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
  const remainder = String(seconds % 60).padStart(2, "0");
  byId("timer").textContent = recording === "requesting" ? "" : minutes + ":" + remainder;
  byId("loading").hidden = !busy;
  byId("loading-label").textContent = t[busy === "voice" ? "listening" : "working"];
  byId("error").hidden = !error;
  byId("error").textContent = error ? t[error] : "";
  byId("connection").textContent = t[connection];
  byId("connection").dataset.online = String(connection === "online");
}

function updateAppointments() {
  const link = byId("panel-link");
  link.textContent = labels[language][pending ? "review" : "appointments"];
  link.dataset.pending = String(Boolean(pending));
  renderAppointments({ root: appointments, language, records, pending,
    busy: Boolean(busy) || recording !== "idle", onDecision: decide });
}

function choose(next, focus = true) {
  language = next;
  try { localStorage.setItem("zaytoun.language", language); } catch {}
  document.documentElement.lang = language;
  document.documentElement.dir = language === "ar" ? "rtl" : "ltr";
  document.title = labels[language].brand;
  byId("welcome").hidden = true;
  byId("workspace").hidden = false;
  byId("header-actions").hidden = false;
  const toggle = byId("language");
  toggle.lang = language === "ar" ? "en" : "ar";
  toggle.textContent = language === "ar" ? "English" : "العربية";
  for (const node of document.querySelectorAll("[data-label]")) {
    node.textContent = labels[language][node.dataset.label];
  }
  message.placeholder = labels[language].message;
  message.setAttribute("aria-label", labels[language].message);
  history.setAttribute("aria-label", labels[language].conversation);
  for (const replay of history.querySelectorAll(".replay")) accessibleLabel(replay, labels[language].replay);
  updateControls();
  updateAppointments();
  if (focus) byId("chat-title").focus();
}

function scrollToLatest() {
  const scroll = byId("chat-scroll");
  scroll.scrollTop = scroll.scrollHeight;
}

function stopSpeech() {
  playbackGeneration += 1;
  speechStream.stop();
  speechIssue = null;
}

async function play(data, replyReceived) {
  stopSpeech();
  const generation = playbackGeneration;
  const playback = speechStream.play(data, replyReceived);
  updateControls();
  const result = await playback;
  if (generation !== playbackGeneration || result === "played" || result === "cancelled") return;
  console.info("clinic_speech_outcome", result);
  speechStream.stop();
  speechIssue = {
    interrupted: "speechInterrupted",
    ticket_unavailable: "speechExpired",
  }[result] ?? "speechBlocked";
  updateControls();
}

function addMessage(kind, text, audio) {
  if (!text) return;
  byId("intro").hidden = true;
  const bubble = document.createElement("article");
  bubble.className = kind === "assistant" ? "message assistant" : "message user " + kind;
  const name = document.createElement("small");
  name.dataset.label = kind === "text" ? "you" : kind;
  name.textContent = labels[language][name.dataset.label];
  const content = document.createElement("p");
  content.dir = "auto";
  content.textContent = text;
  bubble.append(name, content);
  if (audio?.speech_token) {
    const replay = document.createElement("button");
    replay.className = "icon-button replay";
    replay.type = "button";
    replay.innerHTML = '<svg aria-hidden="true"><use href="#speaker"></use></svg>';
    accessibleLabel(replay, labels[language].replay);
    replay.addEventListener("click", () => {
      speechStream.unlock();
      void play(audio);
    });
    bubble.append(replay);
  }
  history.append(bubble);
  scrollToLatest();
}

async function submit(kind, path, fields) {
  if (busy) return;
  stopSpeech();
  busy = kind;
  error = null;
  updateControls();
  updateAppointments();
  scrollToLatest();
  try {
    const response = await fetch(path, {
      method: "POST", cache: "no-store", signal: AbortSignal.timeout(125_000),
      headers: { "X-Clinic-Language": language,
        ...(fields instanceof FormData ? {} : { "Content-Type": "application/json" }) },
      body: fields instanceof FormData ? fields : JSON.stringify({ session_id: session, ...fields }),
    });
    connection = "online";
    const data = await response.json();
    const replyReceived = performance.now();
    if (kind === "voice" && typeof data.synthetic_phone_transcript === "string") {
      window.alert([data.validation, data.phone_issue, data.synthetic_phone_transcript].filter(Boolean).join("\n"));
      return;
    }
    if (!response.ok) {
      const sttFailure = kind === "voice" && ["stt_provider_error", "stt_provider_timeout", "stt_deadline", "stt_exception"].includes(data.category);
      error = sttFailure ? "sttRetry" : response.status === 504 ? "timeout"
        : kind === "voice" && response.status === 422 ? (data.category === "phone_transcript_rejected" ? "phoneTranscription" : "transcription")
          : kind === "voice" && response.status === 413 ? "tooLong" : "requestError";
      if (data.speech_token) {
        if (!muted) void play(data, replyReceived);
        addMessage("assistant", data.reply || labels[language][error], data);
      }
      return;
    }
    continuation = true;
    pending = data.appointment_review ?? null;
    if (Array.isArray(data.session_appointments)) records = data.session_appointments;
    if (data.session_reset) error = "reset";
    if (!muted && data.speech_token) void play(data, replyReceived);
    if (kind === "voice") addMessage("voice", data.transcript);
    addMessage("assistant", data.reply, data);
  } catch (failure) {
    if (failure.name === "TimeoutError") error = "timeout";
    else {
      connection = "offline";
      error = "requestError";
    }
  } finally {
    busy = null;
    updateControls();
    updateAppointments();
    scrollToLatest();
  }
}

function decide(actionId, approved) {
  if (recording !== "idle" || pending?.action_id !== actionId) return;
  if (!muted) speechStream.unlock();
  void submit("approval", "/api/approve", { action_id: actionId, approved });
}

const recorder = createRecorder({
  onState(state, elapsed) {
    const changed = recording !== state;
    recording = state;
    seconds = elapsed;
    updateControls();
    if (changed) updateAppointments();
  },
  onAudio(blob, capturedLanguage) {
    const body = new FormData();
    body.set("session_id", session);
    body.set("language_hint", capturedLanguage);
    body.set("continuation", String(continuation));
    body.set("audio", blob, "recording.wav");
    if (["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
      const probe = new URLSearchParams(location.hash.slice(1)).get("synthetic-phone-probe");
      if (probe) {
        window.history.replaceState(null, "", location.pathname + location.search);
        body.set("synthetic_phone_probe", probe);
      }
    }
    void submit("voice", "/api/voice", body);
  },
  onError(code) {
    error = code === "recording" ? "recordingError" : code;
    updateControls();
    mic.focus();
  },
});

byId("choose-en").addEventListener("click", () => choose("en"));
byId("choose-ar").addEventListener("click", () => choose("ar"));
byId("language").addEventListener("click", () => choose(language === "ar" ? "en" : "ar", false));
byId("spoken-replies").addEventListener("click", () => {
  muted = !muted;
  try { localStorage.setItem("zaytoun.muted", String(muted)); } catch {}
  if (muted) stopSpeech();
  updateControls();
});
mic.addEventListener("click", () => {
  if (busy) return;
  if (recording === "recording") {
    if (!muted) speechStream.unlock();
    recorder.stop();
  }
  else if (recording === "idle") {
    error = null;
    stopSpeech();
    void recorder.start(language);
  }
});
cancel.addEventListener("click", () => {
  recorder.cancel();
  mic.focus();
});
byId("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = message.value.trim();
  if (!text || busy || recording !== "idle") return;
  if (!muted) speechStream.unlock();
  message.value = "";
  addMessage("text", text);
  void submit("text", "/api/chat", { message: text, continuation });
  message.focus();
});
message.addEventListener("input", updateControls);
message.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    byId("composer").requestSubmit();
  }
});
window.addEventListener("pagehide", () => {
  recorder.cancel();
  stopSpeech();
});

if (selectedLanguage === "ar" || selectedLanguage === "en") choose(selectedLanguage, false);
fetch("/health", { cache: "no-store" })
  .then((response) => { connection = response.ok ? "online" : "offline"; })
  .catch(() => { connection = "offline"; })
  .finally(updateControls);
