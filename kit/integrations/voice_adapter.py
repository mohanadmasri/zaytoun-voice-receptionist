# طبقة الربط بين API ومحولي الصوت الموجودين في voice/ دون إعادة كتابتهما.
# المدخل تسجيل WAV/MP3 أو نص الرد؛ المخرج transcript أو حقول الصوت للعقد.
# التفريغ يستخدم OpenRouter الموجود؛ النطق في هذه المرحلة من المتصفح فقط.
# لا تستدعي أدوات العيادة أو LLM الخاص بالحجز، ولا تنزل نماذج أصوات.
import asyncio
import base64
import io
import os
import wave

from voice import STTError, speak, transcribe
from voice.stt import sniff_format


MAX_AUDIO_BYTES = 10 * 1024 * 1024
STT_TIMEOUT_SECONDS = 45
TTS_TIMEOUT_SECONDS = 5
EMPTY_AUDIO = {"audio_b64": None, "audio_mime": None}


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
        except (wave.Error, EOFError, ValueError, RuntimeError, OverflowError):
            raise VoiceError(422, "The WAV recording could not be read. Please record again.") from None


# توفر دوال async قابلة للاستبدال في الاختبارات؛ تنقل العمل المتزامن إلى thread.
class VoiceAdapter:
    # تستقبل التسجيل كـbytes وتعيد النص الأصلي كما فرغه STT دون تبديل أسماء أو أرقام.
    # المهلة تمنع تعليق HTTP؛ انتهاء المهلة لا يستطيع إيقاف thread الشبكة بالقوة.
    async def transcribe(self, data: bytes) -> str:
        validate_audio(data)
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(transcribe, data), timeout=STT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise VoiceError(504, "Speech recognition took too long. Please try again or type your message.") from None
        except STTError:
            raise VoiceError(503, "Speech recognition is unavailable. Please try again or type your message.") from None
        except Exception:
            raise VoiceError(503, "The recording could not be transcribed. Please try again or type your message.") from None
        if not isinstance(text, str) or not text.strip():
            raise VoiceError(422, "Sorry, I didn't catch that. Please record again or type your message.")
        if len(text) > 8000:
            raise VoiceError(422, "The transcript is too long. Please use a shorter recording.")
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
