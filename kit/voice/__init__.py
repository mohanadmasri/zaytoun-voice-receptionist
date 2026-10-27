"""Voice adapters for the receptionist: speech-to-text (voice.stt) and text-to-speech (voice.tts).

    from voice import transcribe, speak
"""
__all__ = ["STTError", "detect_language", "speak", "transcribe"]


def __getattr__(name: str):  # lazy imports keep `python -m voice.tts` and `python -m voice.stt` clean
    if name in ("transcribe", "STTError"):
        from . import stt

        return getattr(stt, name)
    if name in ("speak", "detect_language"):
        from . import tts

        return getattr(tts, name)
    raise AttributeError(name)
