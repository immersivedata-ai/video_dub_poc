from typing import List, Dict, Any, Tuple
from deepgram import DeepgramClient
from core.config import DEEPGRAM_API_KEY
from core.logger import get_logger

log = get_logger("transcribe")

def transcribe_audio(audio_path: str, enable_diarization: bool = True) -> Tuple[List[Dict[str, Any]], str]:
    """
    Transcribes a local audio file using Deepgram Nova-3
    with auto language detection and speaker diarization.

    Returns:
        (segments, detected_language) — e.g. "en", "hi", "es"
    """
    log.info("Transcribing with Deepgram Nova-3 (detect_language=True, diarization=%s)", enable_diarization)

    api_key = DEEPGRAM_API_KEY
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not found.")

    deepgram = DeepgramClient(api_key=api_key)

    with open(audio_path, "rb") as audio_file:
        response = deepgram.listen.v1.media.transcribe_file(
            request=audio_file.read(),
            model="nova-3",
            smart_format=True,
            punctuate=True,
            utterances=True,
            diarize=enable_diarization,
            detect_language=True
        )

    # Extract detected language
    detected_lang = "en"
    if (response.results and response.results.channels
            and response.results.channels[0].detected_language):
        detected_lang = response.results.channels[0].detected_language
    log.info("Detected language: %s", detected_lang)

    segments: List[Dict[str, Any]] = []

    # Process utterances
    if response.results and response.results.utterances:
        for utt in response.results.utterances:
            segments.append({
                "start": float(utt.start),
                "end": float(utt.end),
                "transcript": utt.transcript.strip(),
                "speaker": int(utt.speaker) if hasattr(utt, 'speaker') else 0
            })
        speakers = set(seg["speaker"] for seg in segments)
        log.info("Found %d segments, %d speaker(s)", len(segments), len(speakers))
        return segments, detected_lang

    # Fallback: paragraphs
    if (response.results and response.results.channels
            and response.results.channels[0].alternatives):
        alt = response.results.channels[0].alternatives[0]
        if alt.paragraphs and alt.paragraphs.paragraphs:
            for para in alt.paragraphs.paragraphs:
                segments.append({
                    "start": float(para.start),
                    "end": float(para.end),
                    "transcript": " ".join(
                        sentence.text for sentence in para.sentences
                    ).strip(),
                    "speaker": 0
                })
            log.info("Found %d paragraphs (fallback)", len(segments))
            return segments, detected_lang

        # Final fallback
        if alt.transcript and alt.transcript.strip():
            log.info("Plain transcript: %s...", alt.transcript[:80])
            return [{"start": 0.0, "end": 0.0, "transcript": alt.transcript.strip(), "speaker": 0}], detected_lang

    log.warning("No speech detected in audio")
    return [], detected_lang
