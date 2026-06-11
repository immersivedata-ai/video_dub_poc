import os
import uuid
import json
import shutil
import asyncio
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse

from core.config import MAX_UPLOAD_MB
from core.audioextractor import extract_audio
from core.separator import separate_audio
from core.transcribe import transcribe_audio
from core.translator import Translator
from core.dubbing import generate_dubbed_audio
from core import storage
from core.logger import get_logger
from core.elevenlabs_client import get_credits

log = get_logger("main")
app = FastAPI(title="Dubbing Studio")
CLOUD_RUN = bool(os.getenv("K_SERVICE", ""))

BASE_DIR = Path(os.getenv("STORAGE_DIR", os.getcwd()))
UPLOAD_DIR = BASE_DIR / "input"
AUDIO_DIR = BASE_DIR / "audio"
OUTPUT_DIR = BASE_DIR / "output"
for d in [UPLOAD_DIR, AUDIO_DIR, OUTPUT_DIR]:
    d.mkdir(parents=True, exist_ok=True)

jobs: dict = {}


def run_pipeline(job_id: str, video_path: str, target_lang: str = "Hindi"):
    progress = jobs[job_id]
    log.info("[job %s] Starting pipeline | target=%s", job_id, target_lang)
    try:
        original_audio = str(AUDIO_DIR / f"original_{job_id}.wav")
        dubbed_audio = str(AUDIO_DIR / f"dubbed_{job_id}.aac")
        output_video = str(OUTPUT_DIR / f"dubbed_{job_id}.mp4")

        progress["step"] = 1
        progress["message"] = "Extracting audio from video..."
        extract_audio(video_path, original_audio)

        progress["step"] = 2
        progress["message"] = "Separating vocals from background (Demucs)..."
        vocals_path, background_path = separate_audio(original_audio)

        progress["step"] = 3
        progress["message"] = "Transcribing vocals (Deepgram)..."
        utterances, source_lang = transcribe_audio(vocals_path)
        progress["message"] = f"Transcribed {len(utterances)} utterances ({source_lang})"
        log.info("[job %s] Source language: %s | %d segments", job_id, source_lang, len(utterances))

        progress["step"] = 4
        progress["message"] = "Detecting speaker genders..."
        translator = Translator()
        gender_map = translator.detect_genders(utterances)
        log.info("[job %s] Gender map: %s", job_id, gender_map)

        progress["step"] = 5
        progress["message"] = f"Translating {source_lang} -> {target_lang} (Gemini)..."
        translated = translator.translate_segments(utterances, source_lang=source_lang, target_lang=target_lang)
        progress["message"] = f"Translated {len(translated)} segments"

        progress["step"] = 6
        progress["message"] = "Generating TTS and mixing audio..."
        generate_dubbed_audio(background_path, translated, dubbed_audio, gender_map=gender_map)

        progress["step"] = 7
        progress["message"] = "Merging dubbed audio with video..."
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path, "-i", dubbed_audio,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "copy", "-shortest", output_video
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        progress["step"] = 8
        progress["message"] = "Uploading to cloud..."
        if storage.is_configured():
            gcs_path = f"dubbed/{job_id}.mp4"
            storage.upload_file(output_video, gcs_path)
            progress["output"] = storage.signed_url(gcs_path)
            log.info("[job %s] Uploaded to GCS: %s", job_id, gcs_path)
        else:
            progress["output"] = output_video

        # Cleanup
        for p in [video_path, original_audio, output_video, dubbed_audio]:
            try: os.remove(p)
            except OSError: pass
        try: shutil.rmtree(str(AUDIO_DIR / "separated"), ignore_errors=True)
        except OSError: pass

        progress["message"] = "Dubbing complete!"
        progress["done"] = True
        log.info("[job %s] Pipeline complete", job_id)

    except Exception as e:
        log.exception("[job %s] Pipeline failed: %s", job_id, e)
        progress["error"] = str(e)
        progress["done"] = True


async def event_stream(job_id: str):
    while True:
        p = jobs.get(job_id, {})
        yield f"data: {json.dumps(p)}\n\n"
        if p.get("done"):
            break
        await asyncio.sleep(0.5)


# ── Endpoints ──────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/credits")
async def credits():
    return get_credits()


@app.get("/upload-url")
async def upload_url(filename: str, content_type: str = "video/mp4"):
    if not storage.is_configured():
        raise HTTPException(500, "GCS not configured")
    job_id = uuid.uuid4().hex[:12]
    ext = Path(filename).suffix or ".mp4"
    gcs_path = f"uploads/{job_id}{ext}"
    url = storage.upload_signed_url(gcs_path, content_type)
    log.info("[job %s] Issued upload URL for %s", job_id, filename)
    return {"upload_url": url, "gcs_path": gcs_path, "job_id": job_id}


@app.post("/process")
async def process(gcs_path: str, lang: str = "Hindi"):
    if not storage.is_configured():
        raise HTTPException(500, "GCS not configured")

    job_id = gcs_path.split("/")[-1].rsplit(".", 1)[0]
    local_path = str(UPLOAD_DIR / os.path.basename(gcs_path))
    storage.download_to_path(gcs_path, local_path)

    log.info("[job %s] Downloaded from GCS: %s", job_id, gcs_path)
    jobs[job_id] = {"step": 0, "message": "Starting...", "done": False}
    threading.Thread(target=run_pipeline, args=(job_id, local_path, lang)).start()
    return {"job_id": job_id}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_TEMPLATE


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    return StreamingResponse(event_stream(job_id), media_type="text/event-stream")


@app.get("/download/{job_id}")
async def download(job_id: str):
    p = jobs.get(job_id, {})
    output = p.get("output")
    if not output:
        return {"error": "File not ready"}
    if output.startswith("http"):
        return RedirectResponse(url=output)
    if os.path.exists(output):
        return FileResponse(output, filename=os.path.basename(output))
    return {"error": "File not ready"}


# ── Frontend ──────────────────────────────

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
        .gradient-border {
            border: 1px solid transparent;
            background: linear-gradient(#fff, #fff) padding-box,
                        linear-gradient(135deg, #45d0bd33, #44b6e933) border-box;
        }
        .upload-drop {
            border: 2px dashed #d1dbe6; border-radius: 20px;
            background: #f9fbfc; transition: all 0.3s ease;
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
        <div class="bg-white rounded-[28px] p-8 sm:p-10 gradient-border relative overflow-hidden" style="box-shadow: 0 0 40px rgba(69,208,189,0.08), 0 0 80px rgba(68,182,233,0.04)">
            <div class="absolute top-0 right-0 w-64 h-64 rounded-full blur-3xl opacity-[0.06]" style="background: linear-gradient(135deg, #45d0bd, #44b6e9)"></div>
            <div class="absolute bottom-0 left-0 w-48 h-48 rounded-full blur-3xl opacity-[0.04]" style="background: linear-gradient(135deg, #44b6e9, #45d0bd)"></div>
            <div class="relative z-10">
                <!-- Header -->
                <div class="flex items-center justify-between mb-8">
                    <div class="flex items-center gap-3">
                        <div class="w-10 h-10 rounded-xl flex items-center justify-center" style="background: linear-gradient(135deg, rgba(69,208,189,0.12), rgba(68,182,233,0.12))">
                            <img src="https://immersivedata.ai/wp-content/uploads/2025/08/ID_1-only-logo.svg" alt="ID" class="w-6 h-6" onerror="this.style.display='none'" />
                        </div>
                        <div>
                            <h1 class="text-xl font-bold text-[#0f172a] tracking-tight leading-tight headline">Dubbing Studio</h1>
                            <p class="text-[13px] text-[#64748b] font-medium">Powered by ImmersiveData</p>
                        </div>
                    </div>
                    <div id="credits-badge" class="hidden flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold">
                        <span id="credits-dot" class="w-2 h-2 rounded-full"></span>
                        <span id="credits-text">Loading...</span>
                    </div>
                </div>

                <!-- Upload Zone -->
                <div id="upload-zone">
                    <label for="file-input" id="drop-zone" class="upload-drop flex flex-col items-center justify-center w-full h-56 cursor-pointer group">
                        <div class="w-16 h-16 rounded-2xl flex items-center justify-center mb-4 group-hover:scale-110 transition-transform duration-300" style="background: linear-gradient(135deg, rgba(69,208,189,0.08), rgba(68,182,233,0.06))">
                            <svg class="w-8 h-8 text-[#45d0bd]" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.7" d="M12 4.5v15m0 0l6.75-6.75M12 19.5l-6.75-6.75"/></svg>
                        </div>
                        <p class="text-[#1e293b] font-semibold text-[15px]">Upload your video</p>
                        <p class="text-[#94a3b8] text-[13px] mt-1">Drag & drop or click to browse &middot; Up to 100MB &middot; MP4, MOV, AVI</p>
                        <input id="file-input" type="file" accept="video/*" class="hidden" />
                    </label>
                </div>

                <!-- Language -->
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
                        <div class="progress-track"><div id="progress-bar" class="progress-fill" style="width:0%"></div></div>
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
                        <a id="download-link" href="#" class="btn btn-gradient"><svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.5v15m0 0l6.75-6.75M12 19.5l-6.75-6.75"/></svg>Download Video</a>
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

        fetch('/credits').then(r => r.json()).then(c => {
            const badge = document.getElementById('credits-badge'), dot = document.getElementById('credits-dot'), txt = document.getElementById('credits-text');
            if (c.error) return;
            const pct = c.remaining_pct || 0;
            if (pct > 30) { dot.className = 'w-2 h-2 rounded-full bg-green-400'; badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-green-50 text-green-700'; }
            else if (pct > 10) { dot.className = 'w-2 h-2 rounded-full bg-amber-400'; badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-700'; }
            else { dot.className = 'w-2 h-2 rounded-full bg-red-400'; badge.className = 'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-red-50 text-red-700'; }
            txt.textContent = pct + '% credits left';
        }).catch(()=>{});

        const dz = document.getElementById('drop-zone');
        dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
        dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
        dz.addEventListener('drop', e => {
            e.preventDefault(); dz.classList.remove('dragover');
            const f = e.dataTransfer.files[0];
            if (f) { document.getElementById('file-input').files = e.dataTransfer.files; startUpload(f); }
        });
        document.getElementById('file-input').addEventListener('change', function() { if (this.files[0]) startUpload(this.files[0]); });

        async function startUpload(file) {
            const lang = document.getElementById('target-lang').value;
            document.getElementById('upload-zone').classList.add('hidden');
            document.getElementById('lang-selector').classList.add('hidden');
            document.getElementById('progress-panel').classList.remove('hidden');
            updateStatus('Getting upload URL...', 0);

            try {
                // Step 1: Get signed GCS upload URL
                const res = await fetch('/upload-url?filename=' + encodeURIComponent(file.name) + '&content_type=' + encodeURIComponent(file.type || 'video/mp4'));
                if (!res.ok) throw new Error((await res.json()).detail || 'Failed to get upload URL');
                const {upload_url, gcs_path, job_id} = await res.json();
                currentJobId = job_id;

                // Step 2: Upload directly to GCS (bypasses Cloud Run size limit)
                updateStatus('Uploading to cloud...', 5);
                const up = await fetch(upload_url, { method: 'PUT', body: file, headers: { 'Content-Type': file.type || 'video/mp4' } });
                if (!up.ok) {
                    const errText = await up.text();
                    console.error("GCS Upload Error:", errText);
                    throw new Error('Upload to GCS failed (status ' + up.status + '): ' + errText);
                }

                // Step 3: Start processing
                updateStatus('Starting pipeline...', 12);
                const pr = await fetch('/process?gcs_path=' + encodeURIComponent(gcs_path) + '&lang=' + encodeURIComponent(lang), { method: 'POST' });
                if (!pr.ok) throw new Error((await pr.json()).detail || 'Failed to start processing');
                listenProgress(job_id);
            } catch(e) { showError(e.message); }
        }

        function listenProgress(jobId) {
            const es = new EventSource('/progress/' + jobId);
            es.onmessage = function(e) {
                const p = JSON.parse(e.data);
                if (p.error) { showError(p.error); es.close(); return; }
                const s = p.step || 0;
                updateStatus(p.message, Math.round((s / 8) * 100));
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
