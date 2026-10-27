"""Text-to-speech for the receptionist.

    speak(text, language="en") -> (audio_bytes, mime_type) | None

TTS_PROVIDER (env):
  piper    default. Offline neural voices: English en_US-ljspeech-medium (LJ Speech dataset, public
           domain), Arabic ar_JO-kareem-medium (licence unclear, see the README). Each voice downloads
           once (about 63 MB) into voice/models/ (git-ignored) from the rhasspy/piper-voices repository
           on Hugging Face. Returns WAV bytes. Optional: PIPER_VOICE_EN=en_US-lessac-medium (research-only
           dataset licence, do not use it in a published demo).
  browser  returns None: the UI speaks the reply with the browser's speechSynthesis.

The voice follows the text's script: Arabic letters -> Arabic voice, otherwise English.
`language` is only used when the text has no letters. speak() returns None (and logs a
warning) when synthesis fails, so the caller can fall back to the browser voice.

    python -m voice.tts --download                               # fetch both voices now
    python -m voice.tts "Your appointment is booked." --out reply.wav
"""
from __future__ import annotations

import argparse
import hashlib
import io
import logging
import os
import re
import sys
import threading
import wave
from pathlib import Path

import httpx

log = logging.getLogger("voice.tts")
MODELS_DIR = Path(os.getenv("PIPER_MODELS_DIR") or Path(__file__).resolve().parent / "models")
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
KNOWN_VOICES = {  # name -> (path in the repo, sha256 of the .onnx file)
    "en_US-ljspeech-medium": ("en/en_US/ljspeech/medium", "6f52a751e2349abe7a76735eb09dc1875298c77ea2342ffd2fef79ff81b87f22"),
    "ar_JO-kareem-medium": ("ar/ar_JO/kareem/medium", "9e95cab07b679da603bba17c4dec7ab3111320571964ee95c0379603c086491e"),
    "en_US-lessac-medium": ("en/en_US/lessac/medium", "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f"),
}
ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_voices: dict = {}
_lock = threading.Lock()


def detect_language(text: str, default: str = "en") -> str:
    """'ar' when at least 30% of the letters are Arabic, else 'en' (default when there are no letters)."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return default if default in ("en", "ar") else "en"
    return "ar" if sum(bool(ARABIC.match(c)) for c in letters) / len(letters) >= 0.3 else "en"


def voice_name(language: str) -> str:
    if language == "ar":
        return os.getenv("PIPER_VOICE_AR") or "ar_JO-kareem-medium"
    return os.getenv("PIPER_VOICE_EN") or "en_US-ljspeech-medium"


def voice_status() -> dict[str, bool]:
    """{voice name: already downloaded?} for the English and Arabic voices."""
    return {voice_name(lang): (MODELS_DIR / f"{voice_name(lang)}.onnx").is_file() for lang in ("en", "ar")}


def download(name: str) -> Path:
    """Download a Piper voice (.onnx + .onnx.json) into MODELS_DIR once; verify known checksums."""
    onnx = MODELS_DIR / f"{name}.onnx"
    if onnx.is_file() and (MODELS_DIR / f"{name}.onnx.json").is_file():
        return onnx
    m = re.fullmatch(r"([a-z]+)_([A-Z]+)-([^-]+)-(.+)", name)
    if not m:
        raise ValueError(f"Voice name '{name}' should look like en_US-ljspeech-medium")
    rel = KNOWN_VOICES.get(name, (f"{m[1]}/{m[1]}_{m[2]}/{m[3]}/{m[4]}", None))[0]
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for fname in (f"{name}.onnx.json", f"{name}.onnx"):
        url, part, digest = f"{HF_BASE}/{rel}/{fname}", MODELS_DIR / f"{fname}.part", hashlib.sha256()
        print(f"Downloading Piper voice file {url}", file=sys.stderr)
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with part.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    digest.update(chunk)
        expected = KNOWN_VOICES.get(name, (None, None))[1]
        if fname.endswith(".onnx") and expected and digest.hexdigest() != expected:
            part.unlink()
            raise RuntimeError(f"Checksum mismatch for {fname}: download it again.")
        part.replace(MODELS_DIR / fname)
    return onnx


def _voice(language: str):
    name = voice_name(language)
    if name not in _voices:
        from piper import PiperVoice  # imported lazily: only the piper provider needs it

        _voices[name] = PiperVoice.load(download(name), download_dir=MODELS_DIR)
    return _voices[name]


def _speakable(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:1000]


def speak(text: str, language: str = "en") -> tuple[bytes, str] | None:
    """Synthesize `text`. Returns (wav_bytes, "audio/wav"), or None in browser mode / on failure."""
    provider = (os.getenv("TTS_PROVIDER") or "piper").strip().lower()
    text = _speakable(text or "")
    if provider == "browser" or not text:
        return None
    if provider != "piper":
        log.warning("Unknown TTS_PROVIDER=%r (use piper or browser); no audio returned", provider)
        return None
    try:
        with _lock:
            voice = _voice(detect_language(text, language))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                voice.synthesize_wav(text, wav)
        return buf.getvalue(), "audio/wav"
    except Exception as exc:  # noqa: BLE001 - never break a reply because of TTS
        log.warning("Piper TTS failed (%s); no audio returned", exc)
        return None


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Piper text-to-speech for the receptionist")
    ap.add_argument("text", nargs="?", help="text to speak (Arabic or English)")
    ap.add_argument("--out", default="tts_test.wav", help="output WAV file")
    ap.add_argument("--download", action="store_true", help="download the English and Arabic voices")
    args = ap.parse_args()
    if args.download:
        for lang in ("en", "ar"):
            print(f"{voice_name(lang)}: {download(voice_name(lang))}")
    if args.text:
        os.environ["TTS_PROVIDER"] = "piper"
        out = speak(args.text)
        if not out:
            sys.exit("No audio produced (see the warning above).")
        Path(args.out).write_bytes(out[0])
        print(f"Wrote {args.out} ({len(out[0]) // 1024} KB, voice {voice_name(detect_language(args.text))})")


if __name__ == "__main__":
    main()
