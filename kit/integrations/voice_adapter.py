# طبقة الربط بين API ومحولي الصوت الموجودين في voice/ دون إعادة كتابتهما.
# المدخل تسجيل WAV/MP3 أو نص الرد؛ المخرج transcript أو حقول الصوت للعقد.
# التفريغ يستخدم OpenRouter الموجود؛ النطق في هذه المرحلة من المتصفح فقط.
# لا تستدعي أدوات العيادة أو LLM الخاص بالحجز، ولا تنزل نماذج أصوات.
import asyncio
import base64
import io
import os
import re
import wave
import numpy as np
from time import perf_counter

from voice import STTError, speak, transcribe
from voice.stt import sniff_format
from voice.stt import request_metrics
from agent.patient_identity import normalize_patient_phone, phone_confirmation_reply, spoken_phone_digits


MAX_AUDIO_BYTES = 10 * 1024 * 1024
STT_TIMEOUT_SECONDS = 45
TTS_TIMEOUT_SECONDS = 5
EMPTY_AUDIO = {"audio_b64": None, "audio_mime": None}
RETRY_TRANSCRIPT = "The transcription was unclear. Please record again or type your message."
PROMPT_ECHO_MARKERS = (
    "you are a speech to text engine",
    "transcribe only words actually audible",
    "the recording language selected by the caller",
    "use that language s original script",
    "this is transcription not translation",
    "do not turn a specialty",
    "output only the transcript",
    "preserve leading zeros and repeated digits",
    "this turn collects or confirms a phone number",
    "attend to each spoken digit individually",
    "never fill inaudible digits",
    "عند سماع رقم تلفون انقل كل رقم مسموع بالترتيب",
)


def validate_transcript(text: str, phone_mode: bool) -> None:
    # Reject the whole response; removing artifacts could create a different number.
    words = " ".join(re.findall(r"\w+", text.casefold()))
    clock = r"\d{2}:\d{2}"
    if (any(marker in words for marker in PROMPT_ECHO_MARKERS)
            or re.search(rf"\[\s*{clock}\s*\]|(?<!\d){clock}\s+(?:\d+\s+)?{clock}(?!\d)", text)):
        raise VoiceError(422, RETRY_TRANSCRIPT)
    if phone_mode and phone_confirmation_reply(text) is None:
        # Validate literal digits only. Return the original transcript to the caller.
        digits = spoken_phone_digits(text)
        international = re.fullmatch(r"\+(?:970|972)[\d ().-]+", text.strip())
        if re.search(r"[/\r\n]", text) or not normalize_patient_phone(digits or (text if international else None)):
            raise VoiceError(422, "The phone number was unclear. Please repeat the digits or type the number.")


# تحمل رسالة آمنة للعرض ورمز HTTP؛ لا تتضمن نص استثناء المزود أو مفاتيحه.
class VoiceError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


# تفحص بايتات التسجيل قبل إرسالها إلى STT، ولا تعتمد على اسم الملف أو MIME المرسل.
# ترفض التسجيل الكبير أو الفارغ أو WAV التالف؛ MP3 يُفحص لاحقًا عند المزود.
def validate_audio(data: bytes) -> None:
    if len(data) > MAX_AUDIO_BYTES:
        raise VoiceError(413, "The recording is too large. Please use a shorter recording (up to 10 MB).")
    if len(data) < 1000:
        raise VoiceError(422, "The recording is empty or too short. Please record again or type your message.")
    audio_format = sniff_format(data)
    if audio_format not in ("wav", "mp3"):
        raise VoiceError(415, "Please record or upload WAV or MP3 audio, or type your message.")
    if audio_format == "wav":
        try:
            with wave.open(io.BytesIO(data), "rb") as recording:
                frame_count = recording.getnframes()
                expected_bytes = frame_count * recording.getnchannels() * recording.getsampwidth()
                if frame_count == 0 or len(recording.readframes(frame_count)) != expected_bytes:
                    raise ValueError("Incomplete WAV recording")
                if recording.getsampwidth() != 2:
                    raise ValueError("Unsupported PCM width")
                recording.rewind()
                samples = np.frombuffer(recording.readframes(frame_count), dtype="<i2").astype(np.float64)
                # نفحص المزج الذي سيرسله STT أيضًا؛ قناتان متعاكستان قد تصبحان صمتًا بعد downmix.
                samples = samples.reshape(-1, recording.getnchannels()).mean(axis=1)
                # قياس مستوى الإشارة فقط، لا VAD ولا درجة ثقة تفريغ. لا نرفض عبارة قصيرة حسب كلماتها.
                rms = float(np.sqrt(np.mean(samples * samples)))
                clipped = float(np.mean(np.abs(samples) >= 32760))
                if rms < 20 or clipped > 0.20:
                    raise ValueError("Silent or heavily clipped recording")
        except (wave.Error, EOFError, ValueError, RuntimeError, OverflowError):
            raise VoiceError(422, "The WAV recording could not be read. Please record again.") from None


# توفر دوال async قابلة للاستبدال في الاختبارات؛ تنقل العمل المتزامن إلى thread.
class VoiceAdapter:
    # تستقبل التسجيل كـbytes وتعيد النص الأصلي كما فرغه STT دون تبديل أسماء أو أرقام.
    # المهلة تمنع تعليق HTTP؛ انتهاء المهلة لا يستطيع إيقاف thread الشبكة بالقوة.
    async def transcribe(self, data: bytes, language_hint: str | None = None, phone_mode: bool = False) -> str:
        started = perf_counter()
        validate_audio(data)
        self.timings = {"audio_validation_ms": round((perf_counter() - started) * 1000, 2)}
        started = perf_counter()
        # القياس محلي للـthread الحالي، ولا نقرأ last_usage العامة التي قد تخص جلسة ثانية.
        def run_stt():
            request_metrics.encoding_ms = None
            text = transcribe(data, language_hint, phone_mode=True) if phone_mode else transcribe(data, language_hint)
            return text, request_metrics.encoding_ms
        try:
            text, encoding_ms = await asyncio.wait_for(
                asyncio.to_thread(run_stt), timeout=STT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise VoiceError(504, "Speech recognition took too long. Please try again or type your message.") from None
        except STTError:
            raise VoiceError(503, "Speech recognition is unavailable. Please try again or type your message.") from None
        except Exception:
            raise VoiceError(503, "The recording could not be transcribed. Please try again or type your message.") from None
        self.timings["stt_ms"] = round((perf_counter() - started) * 1000, 2)
        self.timings["audio_encoding_ms"] = encoding_ms
        if not isinstance(text, str) or not text.strip():
            raise VoiceError(422, "Sorry, I didn't catch that. Please record again or type your message.")
        if len(text) > 8000:
            raise VoiceError(422, "The transcript is too long. Please use a shorter recording.")
        validate_transcript(text, phone_mode)
        # نفحص كتابة اللغة قبل عرض النص أو تشغيل Graph؛ لا نعيد ترجمة نتيجة خاطئة.
        letters = [letter for letter in text if letter.isalpha()]
        arabic = sum(bool(re.match(r"[\u0600-\u06ff]", letter)) for letter in letters)
        if letters and ((language_hint == "ar" and arabic / len(letters) < 0.3)
                        or (language_hint == "en" and arabic / len(letters) >= 0.3)):
            raise VoiceError(422, "The transcript does not match the selected recording language. Please record again or type your message.")
        return text.strip()

    # تستقبل رد المساعد وتعيد حقول الصوت؛ None يجعل Gradio ينطق النص بالمتصفح.
    # لا نستدعي Piper حتى لو ضُبط بالخطأ، لأن تنزيل أصوات جديدة غير مصرح به لهذه المرحلة.
    # فشل النطق لا يلغي رد المحادثة ولا يعيد تشغيل Graph.
    async def synthesize(self, reply: str) -> dict:
        if (os.getenv("TTS_PROVIDER") or "browser").strip().lower() != "browser":
            return dict(EMPTY_AUDIO)
        try:
            spoken = await asyncio.wait_for(
                asyncio.to_thread(speak, reply), timeout=TTS_TIMEOUT_SECONDS,
            )
            if spoken is not None:
                return {"audio_b64": base64.b64encode(spoken[0]).decode("ascii"),
                        "audio_mime": spoken[1]}
        except Exception:
            pass
        return dict(EMPTY_AUDIO)
