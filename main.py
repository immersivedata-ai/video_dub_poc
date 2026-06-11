import os
import uuid
import json
import shutil
import asyncio
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse

from core.config import MAX_UPLOAD_MB
from core.audioextractor import extract_audio
from core.separator import separate_audio
from core.transcribe import transcribe_audio
from core.translator import Translator
from core.dubbing import generate_dubbed_audio
from core import storage
from core.logger import get_logger

log = get_logger("main")
app = FastAPI(title="Dubbing Studio")

BASE_DIR = Path(os.getenv("STORAGE_DIR", os.getcwd()))
UPLOAD_DIR = BASE_DIR / "input"
AUDIO_DIR = BASE_DIR / "audio"
OUTPUT_DIR = BASE_DIR / "output"

for d in [UPLOAD_DIR, AUDIO_DIR, OUTPUT_DIR]:
    d.mkdir(parents=True, exist_ok=True)

jobs: dict = {}
MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def run_pipeline(job_id: str, video_path: str, target_lang: str = "Hindi"):
    progress = jobs[job_id]
    log.info("[job %s] Starting pipeline | target=%s | video=%s", job_id, target_lang, video_path)
    try:
        original_audio = str(AUDIO_DIR / f"original_{job_id}.wav")
        dubbed_audio = str(AUDIO_DIR / f"dubbed_{job_id}.aac")
        output_video = str(OUTPUT_DIR / f"dubbed_{job_id}.mp4")

        progress["step"] = 1
        progress["message"] = "Extracting audio from video..."
        log.info("[job %s] Step 1: Extract audio", job_id)
        extract_audio(video_path, original_audio)

        progress["step"] = 2
        progress["message"] = "Separating vocals from background (Demucs)..."
        log.info("[job %s] Step 2: Separate vocals", job_id)
        vocals_path, background_path = separate_audio(original_audio)

        progress["step"] = 3
        progress["message"] = "Transcribing vocals (Deepgram)..."
        log.info("[job %s] Step 3: Transcribe", job_id)
        utterances, source_lang = transcribe_audio(vocals_path)
        progress["message"] = f"Transcribed {len(utterances)} utterances ({source_lang})"
        log.info("[job %s] Transcribed %d segments | source=%s", job_id, len(utterances), source_lang)

        progress["step"] = 4
        progress["message"] = f"Translating {source_lang} -> {target_lang} (Gemini)..."
        log.info("[job %s] Step 4: Translate %s -> %s", job_id, source_lang, target_lang)
        translator = Translator()
        translated = translator.translate_segments(utterances, source_lang=source_lang, target_lang=target_lang)
        progress["message"] = f"Translated {len(translated)} segments"
        log.info("[job %s] Translated %d segments", job_id, len(translated))

        progress["step"] = 5
        progress["message"] = "Generating TTS and mixing audio..."
        log.info("[job %s] Step 5: Generate TTS + mix", job_id)
        generate_dubbed_audio(background_path, translated, dubbed_audio)

        progress["step"] = 6
        progress["message"] = "Merging dubbed audio with video..."
        log.info("[job %s] Step 6: Merge audio + video", job_id)
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
        progress["message"] = "Uploading to cloud..."
        log.info("[job %s] Step 7: Upload to GCS", job_id)

        if storage.is_configured():
            gcs_path = f"dubbed/{job_id}.mp4"
            storage.upload_file(output_video, gcs_path)
            progress["output"] = storage.signed_url(gcs_path)
            log.info("[job %s] Uploaded to GCS: %s", job_id, gcs_path)
        else:
            progress["output"] = output_video
            log.info("[job %s] No GCS configured, serving locally", job_id)

        # Clean up local temp files
        for p in [video_path, original_audio, output_video, dubbed_audio]:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            shutil.rmtree(str(AUDIO_DIR / "separated"), ignore_errors=True)
        except OSError:
            pass

        progress["message"] = "Dubbing complete!"
        progress["done"] = True

        log.info("[job %s] Pipeline complete", job_id)

    except Exception as e:
        log.exception("[job %s] Pipeline failed: %s", job_id, e)
        progress["error"] = str(e)
        progress["done"] = True


async def event_stream(job_id: str):
    while True:
        progress = jobs.get(job_id, {})
        yield f"data: {json.dumps(progress)}\n\n"
        if progress.get("done"):
            break
        await asyncio.sleep(0.5)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_TEMPLATE


@app.post("/upload")
async def upload(file: UploadFile = File(...), lang: str = "Hindi"):
    # Size validation
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    file.file.seek(0)
    if size > MAX_BYTES:
        log.warning("Upload rejected: %s (%.1f MB) exceeds max %d MB", file.filename, size / 1024 / 1024, MAX_UPLOAD_MB)
        raise HTTPException(413, f"File too large. Max {MAX_UPLOAD_MB}MB.")

    job_id = uuid.uuid4().hex[:12]
    ext = Path(file.filename).suffix or ".mp4"
    video_path = str(UPLOAD_DIR / f"input_{job_id}{ext}")
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    log.info("[job %s] Uploaded: %s (%.1f MB) | target=%s", job_id, file.filename, size / 1024 / 1024, lang)
    jobs[job_id] = {"step": 0, "message": "Starting...", "done": False}
    thread = threading.Thread(target=run_pipeline, args=(job_id, video_path, lang))
    thread.start()

    return {"job_id": job_id}


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    return StreamingResponse(event_stream(job_id), media_type="text/event-stream")


@app.get("/download/{job_id}")
async def download(job_id: str):
    progress = jobs.get(job_id, {})
    output = progress.get("output")
    if not output:
        return {"error": "File not ready"}
    # GCS signed URL — redirect; local path — serve directly
    if output.startswith("http"):
        return RedirectResponse(url=output)
    if os.path.exists(output):
        from fastapi.responses import FileResponse
        return FileResponse(output, filename=os.path.basename(output))
    return {"error": "File not ready"}


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dubbing Studio — ImmersiveData</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Sora:wght@400;500;600;700&display=swap');
        * { font-family: 'Manrope', sans-serif; }
        h1, .headline { font-family: 'Sora', sans-serif; }
        body { background: #f8fafb; }
        .glow { box-shadow: 0 0 40px rgba(69,208,189,0.08), 0 0 80px rgba(68,182,233,0.04); }
        .gradient-border {
            border: 1px solid transparent;
            background: linear-gradient(#fff, #fff) padding-box,
                        linear-gradient(135deg, #45d0bd33, #44b6e933) border-box;
        }
        .upload-drop {
            border: 2px dashed #d1dbe6;
            border-radius: 20px;
            background: #f9fbfc;
            transition: all 0.3s ease;
        }
        .upload-drop:hover { border-color: #45d0bd; background: #f0faf8; }
        .upload-drop.dragover { border-color: #45d0bd; background: #e6f7f4; transform: scale(1.01); }
        .step {
            display: flex; align-items: center; gap: 10px;
            padding: 11px 16px; border-radius: 12px;
            font-size: 13px; font-weight: 500;
            color: #94a3b8; background: #f8fafb;
            border: 1.5px solid #e8ecf2; transition: all 0.35s ease;
        }
        .step .dot { width: 8px; height: 8px; border-radius: 50%; background: #cbd5e1; flex-shrink: 0; transition: all 0.35s ease; }
        .step-active { color: #1e293b; background: #f0faf8; border-color: #45d0bd; font-weight: 600; }
        .step-active .dot { background: #45d0bd; box-shadow: 0 0 8px rgba(69,208,189,0.35); }
        .step-done { color: #334155; background: #f1f5f9; border-color: #45d0bd60; }
        .step-done .dot { background: #45d0bd; }
        .progress-track { background: #e2e8f0; border-radius: 10px; overflow: hidden; height: 5px; }
        .progress-fill { background: linear-gradient(113deg, #45d0bd 2.7%, #44b6e9 98.55%); height: 100%; border-radius: 10px; transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1); }
        .btn {
            display: inline-flex; align-items: center; gap: 8px;
            padding: 14px 36px; border-radius: 14px;
            font-weight: 600; font-size: 15px;
            background: #1e293b; color: #fff;
            transition: all 0.25s ease; text-decoration: none;
        }
        .btn:hover { background: #334155; transform: translateY(-1px); box-shadow: 0 8px 24px rgba(30,41,59,0.15); }
        .btn-gradient { background: linear-gradient(113deg, #45d0bd 2.7%, #44b6e9 98.55%); color: #fff; border: none; }
        .btn-gradient:hover { opacity: 0.92; box-shadow: 0 8px 28px rgba(69,208,189,0.3); transform: translateY(-1px); }
        select {
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%2345d0bd' stroke-width='2' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
            background-repeat: no-repeat; background-position: right 16px center; padding-right: 40px !important;
        }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-5">

    <div class="w-full max-w-[620px]">

        <div class="bg-white rounded-[28px] p-8 sm:p-10 gradient-border glow relative overflow-hidden">

            <!-- Subtle bg decoration -->
            <div class="absolute top-0 right-0 w-64 h-64 rounded-full blur-3xl opacity-[0.06]" style="background: linear-gradient(135deg, #45d0bd, #44b6e9)"></div>
            <div class="absolute bottom-0 left-0 w-48 h-48 rounded-full blur-3xl opacity-[0.04]" style="background: linear-gradient(135deg, #44b6e9, #45d0bd)"></div>

            <div class="relative z-10">

                <!-- Header -->
                <div class="flex items-center gap-3 mb-8">
                    <div class="w-10 h-10 rounded-xl flex items-center justify-center" style="background: linear-gradient(135deg, rgba(69,208,189,0.12), rgba(68,182,233,0.12))">
                        <img src="https://immersivedata.ai/wp-content/uploads/2025/08/ID_1-only-logo.svg"
                             alt="ID" class="w-6 h-6" onerror="this.style.display='none'" />
                    </div>
                    <div>
                        <h1 class="text-xl font-bold text-[#0f172a] tracking-tight leading-tight headline">Dubbing Studio</h1>
                        <p class="text-[13px] text-[#64748b] font-medium">Powered by ImmersiveData</p>
                    </div>
                </div>

                <!-- Upload Zone -->
                <div id="upload-zone">
                    <label for="file-input" id="drop-zone" class="upload-drop flex flex-col items-center justify-center w-full h-56 cursor-pointer group">
                        <div class="w-16 h-16 rounded-2xl flex items-center justify-center mb-4 group-hover:scale-110 transition-transform duration-300" style="background: linear-gradient(135deg, rgba(69,208,189,0.08), rgba(68,182,233,0.06))">
                            <svg class="w-8 h-8 text-[#45d0bd]" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.7" d="M12 4.5v15m0 0l6.75-6.75M12 19.5l-6.75-6.75"/></svg>
                        </div>
                        <p class="text-[#1e293b] font-semibold text-[15px]">Upload your video</p>
                        <p class="text-[#94a3b8] text-[13px] mt-1">Drag & drop or click to browse · Max 500MB · MP4, MOV, AVI</p>
                        <input id="file-input" type="file" accept="video/*" class="hidden" onchange="startUpload(this)" />
                    </label>
                </div>

                <!-- Language Selector -->
                <div id="lang-selector" class="mt-5">
                    <label class="block text-[13px] font-semibold text-[#1e293b] mb-2">Target language</label>
                    <select id="target-lang" class="w-full px-4 py-3 bg-[#f8fafb] border border-[#d1dbe6] rounded-xl text-sm text-[#1e293b] font-medium outline-none focus:border-[#45d0bd] focus:ring-3 focus:ring-[#45d0bd]/10 transition-all appearance-none cursor-pointer">
                        <option value="Hindi">Hindi</option>
                        <option value="Tamil">Tamil</option>
                        <option value="Telugu">Telugu</option>
                        <option value="Bengali">Bengali</option>
                        <option value="Marathi">Marathi</option>
                        <option value="Gujarati">Gujarati</option>
                        <option value="Kannada">Kannada</option>
                        <option value="Malayalam">Malayalam</option>
                        <option value="Punjabi">Punjabi</option>
                        <option value="Urdu">Urdu</option>
                        <option value="Odia">Odia</option>
                        <option value="Assamese">Assamese</option>
                    </select>
                </div>

                <!-- Progress Panel -->
                <div id="progress-panel" class="hidden mt-6" style="animation: slideUp 0.4s ease-out">

                    <div class="mb-6">
                        <div class="flex items-center justify-between mb-2.5">
                            <div class="flex items-center gap-2.5">
                                <div class="w-2 h-2 rounded-full animate-pulse" style="background: #45d0bd"></div>
                                <span id="progress-text" class="text-[#1e293b] text-sm font-semibold">Processing...</span>
                            </div>
                            <span id="progress-percent" class="text-[#45d0bd] text-sm font-bold tabular-nums">0%</span>
                        </div>
                        <div class="progress-track">
                            <div id="progress-bar" class="progress-fill" style="width: 0%"></div>
                        </div>
                    </div>

                    <div class="grid grid-cols-2 gap-2.5 mb-6">
                        <div id="step-1" class="step"><span class="dot"></span> Extract Audio</div>
                        <div id="step-2" class="step"><span class="dot"></span> Separate Vocals</div>
                        <div id="step-3" class="step"><span class="dot"></span> Transcribe</div>
                        <div id="step-4" class="step"><span class="dot"></span> Translate</div>
                        <div id="step-5" class="step"><span class="dot"></span> Generate TTS</div>
                        <div id="step-6" class="step"><span class="dot"></span> Merge Video</div>
                    </div>

                    <div id="done-panel" class="hidden text-center py-4" style="animation: slideUp 0.4s ease-out">
                        <div class="w-18 h-18 rounded-2xl flex items-center justify-center mx-auto mb-4" style="background: linear-gradient(135deg, rgba(69,208,189,0.1), rgba(68,182,233,0.08))">
                            <svg class="w-9 h-9 text-[#45d0bd]" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>
                        </div>
                        <p class="text-[#0f172a] text-lg font-bold mb-5">Dubbing complete!</p>
                        <a id="download-link" href="#" class="btn btn-gradient">
                            <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.5v15m0 0l6.75-6.75M12 19.5l-6.75-6.75"/></svg>
                            Download Video
                        </a>
                        <button onclick="resetUI()" class="block mx-auto mt-4 text-[#64748b] hover:text-[#1e293b] text-sm font-medium transition-colors">Dub another video</button>
                    </div>

                    <div id="error-panel" class="hidden text-center py-4" style="animation: slideUp 0.4s ease-out">
                        <div class="w-16 h-16 bg-red-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
                            <svg class="w-8 h-8 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z"/></svg>
                        </div>
                        <p class="text-[#0f172a] text-lg font-bold mb-2">Something went wrong</p>
                        <div id="error-text" class="text-[#64748b] text-sm mb-5 max-w-sm mx-auto leading-relaxed"></div>
                        <button onclick="resetUI()" class="btn">Try Again</button>
                    </div>
                </div>

            </div>
        </div>

        <p class="text-center text-[#94a3b8] text-xs mt-5">Powered by Deepgram &middot; Gemini &middot; ElevenLabs</p>
    </div>

    <style>
        @keyframes slideUp { 0% { opacity: 0; transform: translateY(12px); } 100% { opacity: 1; transform: translateY(0); } }
    </style>

    <script>
        let currentJobId = null;
        const dropZone = document.getElementById('drop-zone');
        dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
        dropZone.addEventListener('drop', e => {
            e.preventDefault(); dropZone.classList.remove('dragover');
            const f = e.dataTransfer.files[0];
            if (f) { document.getElementById('file-input').files = e.dataTransfer.files; startUpload(document.getElementById('file-input')); }
        });
        function startUpload(input) {
            const file = input.files[0]; if (!file) return;
            const lang = document.getElementById('target-lang').value;
            const fd = new FormData(); fd.append('file', file); fd.append('lang', lang);
            document.getElementById('upload-zone').classList.add('hidden');
            document.getElementById('lang-selector').classList.add('hidden');
            document.getElementById('progress-panel').classList.remove('hidden');
            updateStatus('Uploading...', 2);
            fetch('/upload', { method: 'POST', body: fd }).then(r => { if (!r.ok) return r.json().then(e => { throw new Error(e.detail || 'Upload failed') }); return r.json(); }).then(d => { currentJobId = d.job_id; listenProgress(d.job_id); }).catch(e => showError(e.message));
        }
        function listenProgress(jobId) {
            const es = new EventSource('/progress/' + jobId);
            es.onmessage = function(e) {
                const p = JSON.parse(e.data);
                if (p.error) { showError(p.error); es.close(); return; }
                const s = p.step || 0;
                updateStatus(p.message, Math.round((s / 7) * 100));
                for (let i = 1; i <= 6; i++) { const el = document.getElementById('step-' + i); el.classList.remove('step-active','step-done'); if (i < s) el.classList.add('step-done'); else if (i === s) el.classList.add('step-active'); }
                if (p.done && p.output) { es.close(); document.getElementById('done-panel').classList.remove('hidden'); document.getElementById('download-link').href = '/download/' + jobId; }
            };
            es.onerror = () => es.close();
        }
        function updateStatus(m, p) { document.getElementById('progress-text').textContent = m; document.getElementById('progress-percent').textContent = p + '%'; document.getElementById('progress-bar').style.width = p + '%'; }
        function showError(m) { document.getElementById('error-panel').classList.remove('hidden'); document.getElementById('error-text').textContent = m; }
        function resetUI() { currentJobId = null; document.getElementById('upload-zone').classList.remove('hidden'); document.getElementById('lang-selector').classList.remove('hidden'); document.getElementById('progress-panel').classList.add('hidden'); document.getElementById('done-panel').classList.add('hidden'); document.getElementById('error-panel').classList.add('hidden'); document.getElementById('file-input').value = ''; document.getElementById('progress-bar').style.width = '0%'; for (let i = 1; i <= 6; i++) document.getElementById('step-' + i).classList.remove('step-active','step-done'); }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
