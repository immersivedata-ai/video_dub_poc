import os
import uuid
import json
import shutil
import asyncio
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse

from core.audioextractor import extract_audio
from core.separator import separate_audio
from core.transcribe import transcribe_audio
from core.translator import Translator
from core.dubbing import generate_dubbed_audio

app = FastAPI(title="Video Dubbing Studio")

UPLOAD_DIR = Path("input")
AUDIO_DIR = Path("audio")
OUTPUT_DIR = Path("output")
TEMP_DIR = Path("temp_tts")

for d in [UPLOAD_DIR, AUDIO_DIR, OUTPUT_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Track progress per job
jobs: dict = {}

# ────────────────────────────────────────────
# PIPELINE RUNNER (runs in background thread)
# ────────────────────────────────────────────
def run_pipeline(job_id: str, video_path: str, target_lang: str = "Hindi"):
    progress = jobs[job_id]
    try:
        original_audio = str(AUDIO_DIR / f"original_{job_id}.wav")
        dubbed_audio = str(AUDIO_DIR / f"dubbed_{job_id}.aac")
        output_video = str(OUTPUT_DIR / f"dubbed_{job_id}.mp4")

        # STEP 1
        progress["step"] = 1
        progress["message"] = "Extracting audio from video..."
        extract_audio(video_path, original_audio)
        progress["step"] = 1
        progress["message"] = "Audio extracted"

        # STEP 2
        progress["step"] = 2
        progress["message"] = "Separating vocals from background (Demucs)..."
        vocals_path, background_path = separate_audio(original_audio)
        progress["message"] = "Vocals separated"

        # STEP 3
        progress["step"] = 3
        progress["message"] = "Transcribing vocals (Deepgram)..."
        utterances = transcribe_audio(vocals_path)
        progress["message"] = f"Transcribed {len(utterances)} utterances"

        # STEP 4
        progress["step"] = 4
        progress["message"] = f"Translating to {target_lang} (Gemini)..."
        translator = Translator()
        translated = translator.translate_segments(utterances)
        progress["message"] = f"Translated {len(translated)} segments"

        # STEP 5 & 6
        progress["step"] = 5
        progress["message"] = "Generating TTS and mixing audio..."
        generate_dubbed_audio(background_path, translated, dubbed_audio)
        progress["message"] = "TTS generated and mixed"

        # STEP 7
        progress["step"] = 6
        progress["message"] = "Merging dubbed audio with video..."
        subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", dubbed_audio,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "copy",
            "-shortest",
            output_video
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        progress["step"] = 7
        progress["message"] = "Dubbing complete!"
        progress["output"] = output_video
        progress["done"] = True

    except Exception as e:
        progress["error"] = str(e)
        progress["done"] = True


# ────────────────────────────────────────────
# SSE PROGRESS STREAM
# ────────────────────────────────────────────
async def event_stream(job_id: str):
    while True:
        progress = jobs.get(job_id, {})
        yield f"data: {json.dumps(progress)}\n\n"
        if progress.get("done"):
            break
        await asyncio.sleep(0.5)


# ────────────────────────────────────────────
# ROUTES
# ────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_TEMPLATE


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    job_id = uuid.uuid4().hex[:12]
    ext = Path(file.filename).suffix or ".mp4"
    video_path = str(UPLOAD_DIR / f"input_{job_id}{ext}")
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    jobs[job_id] = {"step": 0, "message": "Starting...", "done": False}

    thread = threading.Thread(target=run_pipeline, args=(job_id, video_path))
    thread.start()

    return {"job_id": job_id}


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    return StreamingResponse(event_stream(job_id), media_type="text/event-stream")


@app.get("/download/{job_id}")
async def download(job_id: str):
    progress = jobs.get(job_id, {})
    output_path = progress.get("output")
    if output_path and os.path.exists(output_path):
        return FileResponse(output_path, filename=os.path.basename(output_path))
    return {"error": "File not ready"}


# ────────────────────────────────────────────
# TAILWIND UI (inline HTML)
# ────────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dubbing Studio — ImmersiveData</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    animation: {
                        'fade-in': 'fadeIn 0.5s ease-out',
                        'slide-up': 'slideUp 0.4s ease-out',
                        'pulse-ring': 'pulseRing 2s ease-out infinite',
                    },
                    keyframes: {
                        fadeIn: { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
                        slideUp: { '0%': { opacity: '0', transform: 'translateY(12px)' }, '100%': { opacity: '1', transform: 'translateY(0)' } },
                        pulseRing: { '0%': { boxShadow: '0 0 0 0 rgba(69,208,189,0.4)' }, '70%': { boxShadow: '0 0 0 12px rgba(69,208,189,0)' }, '100%': { boxShadow: '0 0 0 0 rgba(69,208,189,0)' } },
                    },
                }
            }
        }
    </script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        * { font-family: 'Inter', sans-serif; }
        body { background: #ffffff; }
        .card {
            background: #fff;
            border: 1px solid #cacaca;
            border-radius: 20px;
        }
        .upload-drop {
            border: 2px dashed #dadce0;
            border-radius: 16px;
            background: #fafafa;
            transition: all 0.25s ease;
        }
        .upload-drop:hover {
            border-color: #45d0bd;
            background: rgba(69,208,189,0.04);
        }
        .upload-drop.dragover {
            border-color: #45d0bd;
            background: rgba(69,208,189,0.08);
            transform: scale(1.01);
        }
        .step {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 14px;
            border-radius: 10px;
            font-size: 13px;
            font-weight: 500;
            color: #a2a2a2;
            background: #fafafa;
            border: 1.5px solid #f0f0f0;
            transition: all 0.3s ease;
        }
        .step .dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            background: #d7d7d7;
            flex-shrink: 0;
            transition: all 0.3s ease;
        }
        .step-active {
            color: #1f1f1f;
            background: #f0f2f4;
            border-color: #45d0bd;
            font-weight: 600;
        }
        .step-active .dot { background: #45d0bd; box-shadow: 0 0 0 4px rgba(69,208,189,0.18); }
        .step-done {
            color: #333333;
            background: #f4f4f4;
            border-color: #45d0bd;
        }
        .step-done .dot { background: #45d0bd; }
        .progress-track { background: #e5e3df; border-radius: 10px; overflow: hidden; height: 4px; }
        .progress-fill { background: linear-gradient(90deg, #45d0bd, #44b6e9); height: 100%; border-radius: 10px; transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1); }
        .btn {
            display: inline-flex; align-items: center; gap: 8px;
            padding: 14px 32px;
            border-radius: 14px;
            font-weight: 600; font-size: 15px;
            background: #1f1f1f;
            color: #fff;
            transition: all 0.2s ease;
            text-decoration: none;
        }
        .btn:hover { background: #333333; transform: translateY(-1px); box-shadow: 0 8px 24px rgba(0,0,0,0.12); }
        .btn-teal { background: linear-gradient(113deg, #45d0bd 2.7%, #44b6e9 98.55%); color: #fff; border: none; }
        .btn-teal:hover { opacity: 0.92; box-shadow: 0 8px 24px rgba(69,208,189,0.3); }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-5">

    <div class="w-full max-w-[640px] animate-fade-in">

        <!-- Card -->
        <div class="card p-8 sm:p-10">

            <!-- Logo + Brand -->
            <div class="flex items-center gap-3 mb-8">
                <img src="https://immersivedata.ai/wp-content/uploads/2025/08/ID_1-only-logo.svg"
                     alt="ImmersiveData"
                     class="w-9 h-9"
                     onerror="this.style.display='none'" />
                <div>
                    <h1 class="text-xl font-bold text-[#1f1f1f] tracking-tight leading-tight">Dubbing Studio</h1>
                    <p class="text-[13px] text-[#7e7e7e] font-medium">Powered by ImmersiveData</p>
                </div>
            </div>

            <!-- Upload Zone -->
            <div id="upload-zone">
                <label for="file-input"
                       id="drop-zone"
                       class="upload-drop flex flex-col items-center justify-center w-full h-56 cursor-pointer group">
                    <div class="w-14 h-14 rounded-2xl flex items-center justify-center mb-4 group-hover:scale-110 transition-transform duration-300" style="background: rgba(69,208,189,0.08)">
                        <svg class="w-7 h-7 text-[#45d0bd]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 4.5v15m0 0l6.75-6.75M12 19.5l-6.75-6.75" />
                        </svg>
                    </div>
                    <p class="text-[#333333] font-semibold text-[15px]">Upload your video</p>
                    <p class="text-[#a2a2a2] text-[13px] mt-1">Drag & drop or click to browse &middot; MP4, MOV, AVI</p>
                    <input id="file-input" type="file" accept="video/*" class="hidden" onchange="startUpload(this)" />
                </label>
            </div>

            <!-- Progress Panel -->
            <div id="progress-panel" class="hidden mt-6 animate-slide-up">

                <!-- Progress bar -->
                <div class="mb-6">
                    <div class="flex items-center justify-between mb-2.5">
                        <span id="progress-text" class="text-[#333333] text-sm font-semibold">Processing...</span>
                        <span id="progress-percent" class="text-[#45d0bd] text-sm font-bold tabular-nums">0%</span>
                    </div>
                    <div class="progress-track">
                        <div id="progress-bar" class="progress-fill" style="width: 0%"></div>
                    </div>
                </div>

                <!-- Step Indicators -->
                <div class="space-y-1 mb-6">
                    <div class="grid grid-cols-2 gap-2">
                        <div id="step-1" class="step"><span class="dot"></span> Extract Audio</div>
                        <div id="step-2" class="step"><span class="dot"></span> Separate Vocals</div>
                        <div id="step-3" class="step"><span class="dot"></span> Transcribe</div>
                        <div id="step-4" class="step"><span class="dot"></span> Translate</div>
                        <div id="step-5" class="step"><span class="dot"></span> Generate TTS</div>
                        <div id="step-6" class="step"><span class="dot"></span> Merge Video</div>
                    </div>
                </div>

                <!-- Done -->
                <div id="done-panel" class="hidden text-center py-4 animate-slide-up">
                    <div class="w-16 h-16 rounded-2xl flex items-center justify-center mx-auto mb-4" style="background: rgba(69,208,189,0.08)">
                        <svg class="w-8 h-8 text-[#45d0bd]" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>
                    </div>
                    <p class="text-[#1f1f1f] text-lg font-bold mb-5">Dubbing complete!</p>
                    <a id="download-link" href="#" class="btn btn-teal">
                        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.5v15m0 0l6.75-6.75M12 19.5l-6.75-6.75"/></svg>
                        Download Video
                    </a>
                    <button onclick="resetUI()" class="block mx-auto mt-4 text-[#7e7e7e] hover:text-[#1f1f1f] text-sm font-medium transition-colors">Dub another video</button>
                </div>

                <!-- Error -->
                <div id="error-panel" class="hidden text-center py-4 animate-slide-up">
                    <div class="w-16 h-16 bg-red-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
                        <svg class="w-8 h-8 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z"/></svg>
                    </div>
                    <p class="text-[#1f1f1f] text-lg font-bold mb-2">Something went wrong</p>
                    <div id="error-text" class="text-[#7e7e7e] text-sm mb-5 max-w-sm mx-auto leading-relaxed"></div>
                    <button onclick="resetUI()" class="btn">Try Again</button>
                </div>
            </div>
        </div>

        <!-- Footer -->
        <p class="text-center text-[#a2a2a2] text-xs mt-5">Powered by Deepgram &middot; Gemini &middot; ElevenLabs</p>
    </div>

    <script>
        let currentJobId = null;

        const dropZone = document.getElementById('drop-zone');
        dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
        dropZone.addEventListener('drop', e => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
            const file = e.dataTransfer.files[0];
            if (file) { document.getElementById('file-input').files = e.dataTransfer.files; startUpload(document.getElementById('file-input')); }
        });

        function startUpload(input) {
            const file = input.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            document.getElementById('upload-zone').classList.add('hidden');
            document.getElementById('progress-panel').classList.remove('hidden');
            updateStatus('Uploading...', 2);
            fetch('/upload', { method: 'POST', body: formData })
                .then(r => r.json())
                .then(data => { currentJobId = data.job_id; listenProgress(data.job_id); })
                .catch(err => showError('Upload failed: ' + err.message));
        }

        function listenProgress(jobId) {
            const evtSource = new EventSource('/progress/' + jobId);
            evtSource.onmessage = function(e) {
                const p = JSON.parse(e.data);
                if (p.error) { showError(p.error); evtSource.close(); return; }
                const step = p.step || 0;
                const pct = Math.round((step / 7) * 100);
                updateStatus(p.message, pct);
                highlightSteps(step);
                if (p.done && p.output) { evtSource.close(); showDone(jobId); }
            };
            evtSource.onerror = () => evtSource.close();
        }

        function updateStatus(msg, pct) {
            document.getElementById('progress-text').textContent = msg;
            document.getElementById('progress-percent').textContent = pct + '%';
            document.getElementById('progress-bar').style.width = pct + '%';
        }

        function highlightSteps(currentStep) {
            for (let i = 1; i <= 6; i++) {
                const el = document.getElementById('step-' + i);
                el.classList.remove('step-active', 'step-done');
                if (i < currentStep) el.classList.add('step-done');
                else if (i === currentStep) el.classList.add('step-active');
            }
        }

        function showDone(jobId) {
            document.getElementById('done-panel').classList.remove('hidden');
            document.getElementById('download-link').href = '/download/' + jobId;
        }

        function showError(msg) {
            document.getElementById('error-panel').classList.remove('hidden');
            document.getElementById('error-text').textContent = msg;
        }

        function resetUI() {
            currentJobId = null;
            document.getElementById('upload-zone').classList.remove('hidden');
            document.getElementById('progress-panel').classList.add('hidden');
            document.getElementById('done-panel').classList.add('hidden');
            document.getElementById('error-panel').classList.add('hidden');
            document.getElementById('file-input').value = '';
            document.getElementById('progress-bar').style.width = '0%';
            for (let i = 1; i <= 6; i++) {
                const el = document.getElementById('step-' + i);
                el.classList.remove('step-active', 'step-done');
            }
        }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_app:app", host="127.0.0.1", port=7860, reload=True)
