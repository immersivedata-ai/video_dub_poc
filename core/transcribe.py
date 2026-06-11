import os
from typing import List, Dict, Any
from deepgram import DeepgramClient
from core.config import DEEPGRAM_API_KEY

def transcribe_audio(audio_path: str, enable_diarization: bool = True) -> List[Dict[str, Any]]:
    """
    Transcribes a local audio file using Deepgram Nova-3
    with speaker diarization support.

    Output format:
    [
        {"start": 0.0, "end": 2.5, "transcript": "Hello", "speaker": 0},
        {"start": 2.5, "end": 5.0, "transcript": "Hi there", "speaker": 1},
        ...
    ]
    """
    print("Transcribing audio with Deepgram...")
    if enable_diarization:
        print("  Speaker diarization: ENABLED")

    # --- Validate API Key ---
    api_key = DEEPGRAM_API_KEY
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not found. Check your .env file.")

    # --- Initialize Deepgram Client ---
    deepgram = DeepgramClient(api_key=api_key)

    # --- Read Audio File and Transcribe ---
    with open(audio_path, "rb") as audio_file:
        response = deepgram.listen.v1.media.transcribe_file(
            request=audio_file.read(),
            model="nova-3",
            smart_format=True,
            punctuate=True,
            utterances=True,
            diarize=enable_diarization  # Enable speaker diarization
        )

    segments: List[Dict[str, Any]] = []

    # --- Process utterances with speaker IDs ---
    if response.results and response.results.utterances:
        for utt in response.results.utterances:
            segment = {
                "start": float(utt.start),
                "end": float(utt.end),
                "transcript": utt.transcript.strip(),
                "speaker": int(utt.speaker) if hasattr(utt, 'speaker') else 0
            }
            segments.append(segment)
        
        # Print summary
        speakers = set(seg["speaker"] for seg in segments)
        print(f"  [OK] Found {len(segments)} segments with {len(speakers)} speaker(s)")
        return segments

    # --- Fallback: paragraphs (no speaker info) ---
    if (
        response.results
        and response.results.channels
        and response.results.channels[0].alternatives
    ):
        alternative = response.results.channels[0].alternatives[0]

        if alternative.paragraphs:
            for para in alternative.paragraphs.paragraphs:
                segments.append({
                    "start": float(para.start),
                    "end": float(para.end),
                    "transcript": " ".join(
                        sentence.text for sentence in para.sentences
                    ).strip(),
                    "speaker": 0  # Default speaker
                })
            return segments

    # --- Final fallback ---
    if (
        response.results
        and response.results.channels
        and response.results.channels[0].alternatives
    ):
        transcript = response.results.channels[0].alternatives[0].transcript
        if transcript:
            return [{
                "start": 0.0,
                "end": 0.0,
                "transcript": transcript.strip(),
                "speaker": 0
            }]

    return []
