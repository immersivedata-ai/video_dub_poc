import os
import subprocess
import tempfile
import uuid
import time
from typing import List, Dict, Any, Union
from dotenv import load_dotenv
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech
from google.api_core.client_options import ClientOptions
import google.api_core.exceptions
from google.cloud import storage

# Load .env from project root safely
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))


def get_audio_duration(file_path: str) -> float:
    """Returns the duration of an audio file in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return float(result.stdout.strip())
    except (ValueError, IndexError, Exception):
        return 0.0


def split_audio_into_chunks(audio_path: str, chunk_duration: float = 240.0, temp_dir: str = None) -> List[Dict]:
    """
    Splits an audio file into chunks. 
    Using 240s (4 mins) chunks since BatchRecognize can handle longer files more efficiently than Sync.
    """
    total_duration = get_audio_duration(audio_path)
    if total_duration == 0:
        return []
    
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="stt_chunks_")
    
    os.makedirs(temp_dir, exist_ok=True)
    
    chunks = []
    start_time = 0.0
    chunk_index = 0
    
    while start_time < total_duration:
        chunk_path = os.path.join(temp_dir, f"chunk_{chunk_index}.wav")
        
        # Use ffmpeg to extract chunk and convert to LINEAR16 WAV
        cmd = [
            "ffmpeg", "-y",
            "-i", audio_path,
            "-ss", str(start_time),
            "-t", str(chunk_duration),
            "-ar", "16000",
            "-ac", "1",
            "-acodec", "pcm_s16le",
            chunk_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if os.path.exists(chunk_path):
            chunks.append({
                "path": chunk_path,
                "start_offset": start_time
            })
        
        start_time += chunk_duration
        chunk_index += 1
    
    return chunks


def create_recognizer_if_missing(
    client: SpeechClient,
    project_id: str,
    region: str,
    recognizer_id: str,
    language_codes: List[str]
):
    """
    Creates a persistent Recognizer with Chirp 3 and Diarization enabled if it doesn't exist.
    """
    parent = f"projects/{project_id}/locations/{region}"
    resource_path = f"{parent}/recognizers/{recognizer_id}"
    
    try:
        client.get_recognizer(name=resource_path)
        print(f"  [+] Recognizer '{recognizer_id}' already exists.")
        return resource_path
    except google.api_core.exceptions.NotFound:
        print(f"  [+] Creating new recognizer: {recognizer_id}...")
    
    request = cloud_speech.CreateRecognizerRequest(
        parent=parent,
        recognizer_id=recognizer_id,
        recognizer=cloud_speech.Recognizer(
            model="chirp_3",
            language_codes=language_codes,
            default_recognition_config=cloud_speech.RecognitionConfig(
                features=cloud_speech.RecognitionFeatures(
                    enable_word_time_offsets=True,
                    enable_automatic_punctuation=True,
                )
            )
        ),
    )
    
    try:
        operation = client.create_recognizer(request=request)
        operation.result(timeout=120)
        print(f"  [+] Recognizer created successfully.")
        return resource_path
    except Exception as e:
        print(f"  [-] Failed to create recognizer: {e}")
        raise e


def upload_to_gcs(bucket_name: str, source_file_name: str, destination_blob_name: str) -> str:
    """Uploads a file to the bucket and returns the GS URI."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(source_file_name)
    return f"gs://{bucket_name}/{destination_blob_name}"


def delete_from_gcs(bucket_name: str, blob_name: str):
    """Deletes a blob from the bucket."""
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.delete()
    except Exception as e:
        print(f"  [!] Failed to delete GCS blob {blob_name}: {e}")


def transcribe_chunk_batch(
    client: SpeechClient, 
    local_audio_path: str, 
    recognizer_path: str,
    bucket_name: str,
    speaker_count: int = None  # None = auto, else use specified min/max
) -> List[Dict]:
    """
    Transcribes a chunk by uploading to GCS, running BatchRecognize, and parsing inline results.
    """
    # 1. Upload to GCS
    blob_name = f"temp_chunks/{uuid.uuid4()}.wav"
    gcs_uri = upload_to_gcs(bucket_name, local_audio_path, blob_name)
    # print(f"      Uploaded to {gcs_uri}")
    
    try:
        # 2. Batch Recognize
        # Configure diarization based on speaker_count parameter
        if speaker_count is not None:
            # User specified exact speaker count
            min_speakers = max(1, speaker_count)
            max_speakers = max(min_speakers, speaker_count + 1)  # Allow +1 flexibility
            print(f"      Diarization: {min_speakers}-{max_speakers} speakers (user specified)")
        else:
            # Auto-detect: use wide range
            min_speakers = 1
            max_speakers = 7
            print(f"      Diarization: Auto-detect (1-7 speakers)")
        
        diarization_config = cloud_speech.SpeakerDiarizationConfig(
            min_speaker_count=min_speakers,
            max_speaker_count=max_speakers,
        )
        features = cloud_speech.RecognitionFeatures(
            diarization_config=diarization_config,
            enable_word_time_offsets=True,
            enable_automatic_punctuation=True,
        )
        
        config = cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            language_codes=["auto"],
            model="chirp_3",
            features=features,
        )
        
        batch_request = cloud_speech.BatchRecognizeRequest(
            recognizer=recognizer_path,
            config=config,
            files=[cloud_speech.BatchRecognizeFileMetadata(uri=gcs_uri)],
            recognition_output_config=cloud_speech.RecognitionOutputConfig(
                inline_response_config=cloud_speech.InlineOutputConfig()
            ),
        )
        
        operation = client.batch_recognize(request=batch_request)
        print(f"      Job started (Async). Waiting for completion...")
        
        # Poll for completion with feedback
        start_wait = time.time()
        while not operation.done():
            elapsed = int(time.time() - start_wait)
            print(f"\r      ... {elapsed}s elapsed", end="", flush=True)
            time.sleep(5)
            if elapsed > 900: # 15 min timeout per chunk
                 print("\n      [!] Timeout reached.")
                 operation.cancel()
                 break
        print("") # Newline
        
        # Get result
        response = operation.result()
        
        segments = []
        
        # 3. Parse Results
        # Response contains results keyed by URI
        if gcs_uri in response.results:
            file_result = response.results[gcs_uri]
            
            # Check for errors
            if file_result.error and file_result.error.code != 0:
                print(f"      [!] Batch Error for chunk: {file_result.error.message}")
                return []
            
            # Parse transcript
            if file_result.transcript and file_result.transcript.results:
                for result in file_result.transcript.results:
                    if result.alternatives:
                        alternative = result.alternatives[0]
                        # transcript = alternative.transcript # Not used directly if we parse words
                        
                        if alternative.words:
                            current_speaker = 0
                            segment_words = []
                            segment_start = alternative.words[0].start_offset.total_seconds()
                            prev_word_end = segment_start # Initialize
                            
                            # DEBUG: Check first word attributes for speaker tags
                            first_word = alternative.words[0]
                            print(f"      [DEBUG] First word: '{first_word.word}', Speaker Tag: {getattr(first_word, 'speaker_tag', 'Missing')}, Label: {getattr(first_word, 'speaker_label', 'Missing')}")

                            for word in alternative.words:
                                # flexible speaker ID extraction (V1 vs V2 behavior)
                                speaker_tag = getattr(word, 'speaker_tag', 0)
                                speaker_label = getattr(word, 'speaker_label', None)
                                
                                if speaker_tag:
                                    speaker_id = int(speaker_tag)
                                elif speaker_label:
                                    # speaker_label might be "1", "speaker_1", etc.
                                    try:
                                        speaker_id = int(str(speaker_label).replace("speaker_", ""))
                                    except:
                                        speaker_id = 0
                                else:
                                    speaker_id = 0
                                
                                word_start = word.start_offset.total_seconds()
                                word_end = word.end_offset.total_seconds()
                                
                                # Split Condition 1: Speaker Change
                                speaker_changed = (speaker_id != current_speaker)
                                
                                # Split Condition 2: Silence Gap > 0.7s (Natural Pause)
                                silence_gap = (word_start - prev_word_end) if prev_word_end > 0 else 0
                                is_pause = silence_gap > 0.7
                                
                                # Split Condition 3: Segment too long (> 30s) AND pause > 0.3s (Soft split)
                                is_too_long = (word_start - segment_start) > 30.0 and silence_gap > 0.3
                                
                                if (speaker_changed or is_pause or is_too_long) and segment_words:
                                     # End previous segment
                                     segments.append({
                                         "start": segment_start,
                                         "end": prev_word_end,
                                         "speaker": current_speaker,
                                         "transcript": " ".join(segment_words)
                                     })
                                     # Start new segment
                                     current_speaker = speaker_id
                                     segment_words = [word.word]
                                     segment_start = word_start
                                     prev_word_end = word_end
                                else:
                                    if not segment_words:
                                        current_speaker = speaker_id
                                        segment_start = word_start
                                    segment_words.append(word.word)
                                    prev_word_end = word_end
                            
                            if segment_words:
                                 # Use the last word end
                                 segments.append({
                                     "start": segment_start,
                                     "end": prev_word_end,
                                     "speaker": current_speaker,
                                     "transcript": " ".join(segment_words)
                                 })
            else:
                 print(f"      [!] No transcript found in response.")

        return segments

    finally:
        # 4. cleanup GCS
        delete_from_gcs(bucket_name, blob_name)


def transcribe_audio(
    audio_path: str, 
    source_language: str = "multi", 
    enable_diarization: bool = True,
    speaker_count: int = None  # None = auto-detect, or specify 1-7
) -> List[Dict[str, Any]]:
    """
    Transcribes audio using Google Cloud Speech-to-Text v2 API (Chirp 3) via BatchRecognize.
    Uses a temporary GCS bucket for upload/processing.
    """
    print(f"Transcribing audio (Batch Mode) with Google Cloud Speech-to-Text (Source: {source_language})...")
    
    project_id = os.getenv("GCP_PROJECT_ID")
    gcp_region = os.getenv("GCP_REGION", "us")
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    # Get Bucket Name (Env or Fallback)
    bucket_name = os.getenv("GCS_BUCKET_NAME", "dub_poc_bucket")
    
    if not project_id or not credentials_path:
        print("[-] Error: Missing GCP_PROJECT_ID or GOOGLE_APPLICATION_CREDENTIALS.")
        return []
        
    if not os.path.exists(audio_path):
        print(f"[-] Audio file not found: {audio_path}")
        return []

    # ALL_INDIAN_LANGUAGES (Without pa-IN)
    ALL_INDIAN_LANGUAGES = [
        "en-US", "hi-IN", "bn-IN", "ta-IN", "te-IN", 
        "kn-IN", "ml-IN", "gu-IN", "mr-IN", "or-IN"
    ]

    if source_language == "multi":
        # Supports "auto" for universal detection as requested
        language_codes = ["auto"]
        print(f"  Language Detection: Auto Mode (Universal)")
    else:
        single_lang_map = {
            "en": "en-US", "hi": "hi-IN", "ta": "ta-IN", "te": "te-IN",
            "kn": "kn-IN", "ml": "ml-IN", "mr": "mr-IN", "bn": "bn-IN",
            "gu": "gu-IN", "or": "or-IN", "as": "as-IN",
        }
        lang_code = single_lang_map.get(source_language, "en-US")
        language_codes = [lang_code]
        print(f"  Language set to: {lang_code}")
    
    try:
        client_options = ClientOptions(api_endpoint=f"{gcp_region}-speech.googleapis.com")
        client = SpeechClient(client_options=client_options)
        
        print(f"  Using GCS Bucket: {bucket_name}")
        
        RECOGNIZER_ID = "voice-dub-chirp3-diarizer-v7"
        print(f"  --> Ensuring Recognizer '{RECOGNIZER_ID}' exists...")
        recognizer_path = create_recognizer_if_missing(
            client=client,
            project_id=project_id,
            region=gcp_region,
            recognizer_id=RECOGNIZER_ID,
            language_codes=language_codes
        )
        
        total_duration = get_audio_duration(audio_path)
        print(f"  Audio duration: {total_duration:.1f} seconds")
        
        all_segments = []
        
        # Split into chunks (Can use longer chunks now, e.g., 240s)
        # We process chunks sequentially to update user (could be parallelized)
        chunks = split_audio_into_chunks(audio_path, chunk_duration=240.0)
        print(f"  --> Processing {len(chunks)} chunks via BatchRecognize...")
        
        for i, chunk in enumerate(chunks):
            print(f"  --> Processing chunk {i+1}/{len(chunks)} (start: {chunk['start_offset']:.1f}s)...")
            
            try:
                chunk_segments = transcribe_chunk_batch(
                    client=client,
                    local_audio_path=chunk["path"],
                    recognizer_path=recognizer_path,
                    bucket_name=bucket_name,
                    speaker_count=speaker_count
                )
                
                # Adjust timestamps
                if chunk_segments:
                    for seg in chunk_segments:
                        seg["start"] += chunk["start_offset"]
                        seg["end"] += chunk["start_offset"]
                        all_segments.append(seg)
                    print(f"      Got {len(chunk_segments)} segments.")
                
            except Exception as e:
                print(f"      [!] Error processing chunk {i+1}: {e}")
                # Optional: Import traceback and print
            
            # Cleanup local chunk
            try:
                os.remove(chunk["path"])
            except:
                pass
        
        # Cleanup temp dir
        if chunks:
            try:
                os.rmdir(os.path.dirname(chunks[0]["path"]))
            except:
                pass
                
        print(f"  [+] Transcription complete: {len(all_segments)} segments.")
        return all_segments
        
    except Exception as e:
        print(f"[-] Transcription failed: {e}")
        import traceback
        traceback.print_exc()
        return []
