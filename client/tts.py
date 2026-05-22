"""
tts.py — ElevenLabs TTS with edge-tts fallback for The Dispatch.

Voice is selected globally via the DISPATCHER_VOICE env var.
Falls back to edge-tts automatically if ELEVENLABS_API_KEY is missing.
"""

import os
import threading
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '')
DISPATCHER_VOICE   = os.getenv('DISPATCHER_VOICE', 'gruff')

VOICE_PROFILES = {
    'gruff':   '29vD33N1CtxCmqQRPOHJ',  # Drew — deep, masculine
    'radio':   'VR6AewLTigWG4xSOukaG',  # Arnold — gravelly
    'southern': 'pNInz6obpgDQGcFmaJgB', # Adam — warm baritone
}

_el_client = None
_el_lock   = threading.Lock()


def _get_el_client():
    global _el_client
    with _el_lock:
        if _el_client is None and ELEVENLABS_API_KEY:
            from elevenlabs.client import ElevenLabs
            _el_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
        return _el_client


def speak_elevenlabs(text: str, voice_id: str | None = None):
    """
    Speak text via ElevenLabs eleven_turbo_v2.
    Falls back to edge-tts if ELEVENLABS_API_KEY is not set or the call fails.
    """
    if not voice_id:
        voice_id = VOICE_PROFILES.get(DISPATCHER_VOICE, VOICE_PROFILES['gruff'])

    client = _get_el_client()
    if client:
        try:
            from elevenlabs import play
            audio = client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id="eleven_turbo_v2",
                output_format="mp3_44100_128",
            )
            play(audio)
            return
        except Exception as e:
            print(f'[TTS] ElevenLabs error: {e} — falling back to edge-tts', flush=True)

    # ── edge-tts fallback ─────────────────────────────────────────────────────
    import asyncio
    import tempfile
    import edge_tts
    import sounddevice as sd
    import soundfile as sf

    async def _edge_speak():
        tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
        tmp.close()
        try:
            await edge_tts.Communicate(text, 'en-US-GuyNeural').save(tmp.name)
            data, sr = sf.read(tmp.name)
            sd.play(data, sr)
            sd.wait()
        except Exception as e2:
            print(f'[TTS] edge-tts fallback error: {e2}', flush=True)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    asyncio.run(_edge_speak())
