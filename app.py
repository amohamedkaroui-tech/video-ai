import asyncio
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path

import edge_tts
import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageOps

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "generated"
WORK.mkdir(exist_ok=True)

JOBS = {}
LOCK = threading.Lock()

VOICES = {
    "fr": "fr-FR-HenriNeural",
    "ar": "ar-TN-HediNeural",
    "en": "en-US-ChristopherNeural",
}

HEADERS = {
    "User-Agent": "VideoStudioPrototype/1.0"
}

PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mon studio vidéo</title>
<style>
*{box-sizing:border-box}
body{background:#101827;color:#fff;font-family:Arial;margin:0;padding:20px}
main{max-width:600px;margin:auto}
input,textarea,select,button{
width:100%;padding:13px;margin:8px 0 18px;border-radius:9px;
border:1px solid #46536a;font-size:16px
}
input,textarea,select{background:#202d43;color:white}
button{background:#2768e8;color:white;cursor:pointer}
button:disabled{opacity:.5}
video{width:100%;max-height:600px;background:black}
a{color:#8ec5ff;display:block;margin:15px 0}
small{color:#b8c4d6}
</style>
</head>
<body>
<main>
<h1>Mon studio vidéo</h1>
<p>Photos avec zoom animé, voix et sous-titres.</p>

<label>Photos à chercher</label>
<input id="topic" placeholder="Exemple : sea, nature, Tunis">

<label>Texte exact que la voix doit prononcer</label>
<textarea id="script" rows="6" maxlength="650"
placeholder="Écris ici ton texte en français, arabe ou anglais."></textarea>

<label>Langue de la voix</label>
<select id="language">
<option value="fr">Français</option>
<option value="ar">العربية</option>
<option value="en">English</option>
</select>

<small>
Commence avec 2 ou 3 phrases et un sujet visuel simple.
La durée dépend du texte prononcé.
</small>

<button id="button" onclick="generate()">Créer la vidéo</button>
<p id="status"></p>
<video id="player" controls hidden playsinline></video>
<a id="download" hidden>Télécharger la vidéo</a>
<a id="credits" hidden>Informations sur les images</a>
</main>

<script>
const el = id => document.getElementById(id);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function generate(){
  el('button').disabled = true;
  el('player').hidden = true;
  el('download').hidden = true;
  el('credits').hidden = true;
  el('status').textContent = 'Démarrage…';

  try{
    let response = await fetch('/api/generate', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        topic:el('topic').value,
        script:el('script').value,
        language:el('language').value
      })
    });

    let result = await response.json();
    if(!response.ok) throw new Error(result.error || 'Erreur du serveur');

    const id = result.id;

    while(true){
      await sleep(2500);
      response = await fetch('/api/status/' + id);
      if(!response.ok) throw new Error('Suivi interrompu');
      result = await response.json();

      el('status').textContent = result.message || result.status;

      if(result.status === 'error'){
        throw new Error(result.message);
      }

      if(result.status === 'done'){
        el('player').src = result.video;
        el('player').hidden = false;

        el('download').href = result.video + '?download=1';
        el('download').hidden = false;

        el('credits').href = result.credits;
        el('credits').hidden = false;
        break;
      }
    }
  }catch(error){
    el('status').textContent = 'Erreur : ' + error.message;
  }finally{
    el('button').disabled = false;
  }
}
</script>
</body>
</html>
"""


def run_command(command, cwd=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if result.returncode:
        print(result.stderr[-4000:], flush=True)
        raise RuntimeError(
            "Le traitement vidéo a échoué. Consulte les logs du serveur."
        )
    return result.stdout


def update(job_id, message):
    JOBS[job_id]["message"] = message


def find_images(topic, folder):
    response = requests.get(
        "https://api.openverse.org/v1/images/",
        params={
            "q": topic,
            "page_size": 12,
            "license": "cc0,pdm",
        },
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    images = []
    credits = []

    for item in response.json().get("results", []):
        if item.get("license") not in {"cc0", "pdm"}:
            continue

        url = item.get("url")
        if not url:
            continue

        try:
            path = folder / f"image_{len(images)}.jpg"

            with requests.get(
                url,
                headers=HEADERS,
                timeout=20,
                stream=True,
            ) as media:
                media.raise_for_status()
                content = bytearray()

                for chunk in media.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 15 * 1024 * 1024:
                        raise ValueError("Image trop volumineuse")

            from io import BytesIO

            with Image.open(BytesIO(content)) as original:
                original = ImageOps.exif_transpose(original)
                original = original.convert("RGB")
                prepared = ImageOps.fit(
                    original,
                    (720, 1280),
                    method=Image.Resampling.LANCZOS,
                )
                prepared.save(path, quality=88)

            images.append(path)
            credits.append({
                "title": item.get("title"),
                "creator": item.get("creator"),
                "license": item.get("license"),
                "license_url": item.get("license_url"),
                "source_page": item.get("foreign_landing_url"),
            })

            if len(images) == 3:
                break

        except Exception as error:
            print("Image ignorée :", str(error), flush=True)

    if not images:
        raise RuntimeError(
            "Aucune photo téléchargeable trouvée. "
            "Essaie un sujet simple comme nature ou sea."
        )

    (folder / "credits.json").write_text(
        json.dumps(credits, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return images


async def create_voice(text, language, folder):
    words = []
    voice = edge_tts.Communicate(
        text,
        VOICES[language],
        boundary="WordBoundary",
    )

    with open(folder / "voice.mp3", "wb") as output:
        async for event in voice.stream():
            if event["type"] == "audio":
                output.write(event["data"])
            elif event["type"] == "WordBoundary":
                words.append({
                    "start": event["offset"] / 10_000_000,
                    "duration": event["duration"] / 10_000_000,
                    "text": event["text"],
                })

    return words


def ass_time(seconds):
    total = max(0, round(seconds * 100))
    hours, rest = divmod(total, 360000)
    minutes, rest = divmod(rest, 6000)
    seconds, hundredths = divmod(rest, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{hundredths:02}"


def create_subtitles(words, text, duration, folder):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,58,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,45,45,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if not words:
        split = text.split()
        step = duration / max(1, len(split))
        words = [
            {"start": i * step, "duration": step, "text": word}
            for i, word in enumerate(split)
        ]

    lines = []

    for index, word in enumerate(words):
        start = word["start"]
        end = (
            words[index + 1]["start"]
            if index + 1 < len(words)
            else min(duration, start + word["duration"] + 0.3)
        )

        safe = (
            word["text"]
            .replace("\\", "")
            .replace("{", "")
            .replace("}", "")
            .replace("
", " ")
        )

        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},"
            f"Default,,0,0,0,,{safe}"
        )

    (folder / "captions.ass").write_text(
        header + "
".join(lines),
        encoding="utf-8",
    )


def generate_video(job_id, topic, text, language):
    folder = WORK / job_id
    folder.mkdir()

    try:
        update(job_id, "Recherche et préparation des photos…")
        images = find_images(topic, folder)

        update(job_id, "Création de la voix…")
        words = asyncio.run(create_voice(text, language, folder))

        duration = float(run_command([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "voice.mp3",
        ], cwd=folder))

        if duration > 65:
            raise RuntimeError(
                "Texte trop long : raccourcis-le pour rester sous 65 secondes."
            )

        create_subtitles(words, text, duration, folder)

        update(job_id, "Animation des photos…")
        per_scene = duration / len(images)
        frames = max(1, int(per_scene * 24) + 1)

        for index, image in enumerate(images):
            run_command([
                "ffmpeg", "-y",
                "-i", image.name,
                "-vf",
                (
                    "zoompan="
                    "z='min(zoom+0.0007,1.12)':"
                    "x='iw/2-iw/zoom/2':"
                    "y='ih/2-ih/zoom/2':"
                    f"d={frames}:s=720x1280:fps=24"
                ),
                "-frames:v", str(frames),
                "-an",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "26",
                "-pix_fmt", "yuv420p",
                "-threads", "1",
                f"part_{index}.mp4",
            ], cwd=folder)

        (folder / "parts.txt").write_text(
            "
".join(
                f"file 'part_{i}.mp4'" for i in range(len(images))
            ),
            encoding="utf-8",
        )

        run_command([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", "parts.txt",
            "-c", "copy",
            "joined.mp4",
        ], cwd=folder)

        update(job_id, "Ajout des sous-titres et finalisation…")

        run_command([
            "ffmpeg", "-y",
            "-i", "joined.mp4",
            "-i", "voice.mp3",
            "-vf", "ass=captions.ass",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(duration),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "26",
            "-threads", "1",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "video.mp4",
        ], cwd=folder)

        JOBS[job_id].update({
            "status": "done",
            "message": "Vidéo prête.",
            "video": f"/files/{job_id}/video.mp4",
            "credits": f"/files/{job_id}/credits.json",
        })

    except Exception as error:
        print("Erreur :", repr(error), flush=True)
        JOBS[job_id].update({
            "status": "error",
            "message": str(error)[:500],
        })

    finally:
        # Conserver seulement le résultat et les informations des sources.
        for path in folder.iterdir():
            if path.name not in {"video.mp4", "credits.json"}:
                path.unlink(missing_ok=True)
        LOCK.release()


@app.get("/")
def home():
    return PAGE


@app.post("/api/generate")
def generate():
    data = request.get_json(silent=True) or {}

    topic = str(data.get("topic", "")).strip()
    text = str(data.get("script", "")).strip()
    language = data.get("language", "fr")

    if not topic or not text:
        return jsonify(error="Remplis le sujet et le texte à prononcer."), 400

    if len(topic) > 120 or len(text) > 650:
        return jsonify(error="Le sujet ou le texte est trop long."), 400

    if language not in VOICES:
        return jsonify(error="Langue non disponible."), 400

    if not LOCK.acquire(blocking=False):
        return jsonify(
            error="Une génération est déjà en cours. Réessaie plus tard."
        ), 429

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "working",
        "message": "Démarrage…",
    }

    threading.Thread(
        target=generate_video,
        args=(job_id, topic, text, language),
        daemon=True,
    ).start()

    return jsonify(id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    if job_id not in JOBS:
        return jsonify(
            error="Génération introuvable : le serveur a peut-être redémarré."
        ), 404
    return jsonify(JOBS[job_id])


@app.get("/files/<job_id>/<filename>")
def files(job_id, filename):
    if not re_valid_id(job_id):
        return "Introuvable", 404

    if filename not in {"video.mp4", "credits.json"}:
        return "Introuvable", 404

    return send_from_directory(
        WORK / job_id,
        filename,
        as_attachment=request.args.get("download") == "1",
    )


def re_valid_id(value):
    return (
        len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )
