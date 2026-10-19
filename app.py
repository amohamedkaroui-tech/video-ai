import asyncio
import json
import os
import subprocess
import threading
import uuid
from io import BytesIO
from pathlib import Path

import edge_tts
import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageOps


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "generated"
WORK.mkdir(parents=True, exist_ok=True)

JOBS = {}
LOCK = threading.Lock()

VOICES = {
    "fr": "fr-FR-HenriNeural",
    "ar": "ar-TN-HediNeural",
    "en": "en-US-ChristopherNeural",
}

HEADERS = {
    "User-Agent": "VideoStudioPrototype/1.1"
}

NEWLINE = chr(10)


PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mon studio vidéo</title>

<style>
* {
    box-sizing: border-box;
}

body {
    background: #101827;
    color: white;
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 20px;
}

main {
    max-width: 600px;
    margin: auto;
}

input, textarea, select, button {
    width: 100%;
    padding: 13px;
    margin: 8px 0 18px;
    border-radius: 9px;
    border: 1px solid #46536a;
    font-size: 16px;
}

input, textarea, select {
    background: #202d43;
    color: white;
}

button {
    background: #2768e8;
    color: white;
    cursor: pointer;
}

button:disabled {
    opacity: 0.5;
}

video {
    width: 100%;
    max-height: 600px;
    background: black;
}

a {
    color: #8ec5ff;
    margin: 15px 0;
}

small {
    color: #b8c4d6;
}

#status {
    overflow-wrap: anywhere;
}

[hidden] {
    display: none !important;
}
</style>
</head>

<body>
<main>
    <h1>Mon studio vidéo</h1>

    <p>
        Photos avec zoom animé,
        voix et sous-titres.
    </p>

    <label for="topic">Photos à chercher</label>

    <input
        id="topic"
        maxlength="120"
        placeholder="Exemple : sea, nature, Tunis"
    >

    <label for="script">
        Texte exact que la voix doit prononcer
    </label>

    <textarea
        id="script"
        rows="6"
        maxlength="650"
        dir="auto"
        placeholder="Écris ton texte en français, arabe ou anglais."
    ></textarea>

    <label for="language">Langue de la voix</label>

    <select id="language">
        <option value="fr">Français</option>
        <option value="ar">العربية</option>
        <option value="en">English</option>
    </select>

    <small>
        Commence avec deux ou trois phrases.
        La durée dépend du texte prononcé.
        Choisir une langue ne traduit pas le texte.
    </small>

    <button id="button" onclick="generate()">
        Créer la vidéo
    </button>

    <p id="status" role="status"></p>

    <video
        id="player"
        controls
        hidden
        playsinline
    ></video>

    <p>
        <a id="download" hidden>
            Télécharger la vidéo
        </a>
    </p>

    <p>
        <a id="credits" hidden target="_blank" rel="noopener">
            Informations sur les images
        </a>
    </p>
</main>

<script>
const el = id => document.getElementById(id);

const sleep = ms => new Promise(
    resolve => setTimeout(resolve, ms)
);

async function readResponse(response) {
    const type = response.headers.get('content-type') || '';

    if (!type.includes('application/json')) {
        throw new Error(
            'Le serveur ne répond pas correctement. Code HTTP : '
            + response.status
        );
    }

    const data = await response.json();

    if (!response.ok) {
        throw new Error(
            data.error || 'Erreur HTTP ' + response.status
        );
    }

    return data;
}

async function generate() {
    const topic = el('topic').value.trim();
    const script = el('script').value.trim();

    if (!topic || !script) {
        el('status').textContent =
            'Remplis le sujet et le texte à prononcer.';
        return;
    }

    el('button').disabled = true;
    el('player').hidden = true;
    el('download').hidden = true;
    el('credits').hidden = true;

    el('status').textContent = 'Démarrage…';

    try {
        const response = await fetch('/api/generate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                topic: topic,
                script: script,
                language: el('language').value
            })
        });

        const first = await readResponse(response);
        const id = first.id;

        while (true) {
            await sleep(2500);

            const statusResponse = await fetch(
                '/api/status/' + id,
                {cache: 'no-store'}
            );

            const result = await readResponse(statusResponse);

            el('status').textContent =
                result.message || result.status;

            if (result.status === 'error') {
                throw new Error(result.message);
            }

            if (result.status === 'done') {
                el('player').src = result.video;
                el('player').hidden = false;
                el('player').load();

                el('download').href =
                    result.video + '?download=1';

                el('download').hidden = false;

                el('credits').href = result.credits;
                el('credits').hidden = false;

                break;
            }
        }
    } catch (error) {
        el('status').textContent =
            'Erreur : ' + error.message;
    } finally {
        el('button').disabled = false;
    }
}
</script>
</body>
</html>
"""


def run_command(command, cwd=None):
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Le traitement a dépassé le temps autorisé. "
            "Essaie avec un texte plus court."
        ) from error

    if result.returncode != 0:
        print(result.stderr[-6000:], flush=True)

        raise RuntimeError(
            "Le traitement vidéo a échoué. "
            "Consulte les logs Render."
        )

    return result.stdout


def update(job_id, message):
    JOBS[job_id]["message"] = message
    print(f"[{job_id}] {message}", flush=True)


def find_images(topic, folder):
    try:
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

    except requests.RequestException as error:
        raise RuntimeError(
            "La recherche de photos est indisponible "
            "ou limitée temporairement. Réessaie plus tard."
        ) from error

    images = []
    credits = []
    used_urls = set()

    for item in response.json().get("results", []):
        if item.get("license") not in {"cc0", "pdm"}:
            continue

        url = item.get("url")

        if not url or url in used_urls:
            continue

        used_urls.add(url)

        try:
            content = bytearray()

            with requests.get(
                url,
                headers=HEADERS,
                timeout=20,
                stream=True,
            ) as media:
                media.raise_for_status()

                for chunk in media.iter_content(65536):
                    content.extend(chunk)

                    if len(content) > 15 * 1024 * 1024:
                        raise ValueError(
                            "Image trop volumineuse"
                        )

            path = folder / f"image_{len(images)}.jpg"

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

            if len(images) >= 3:
                break

        except Exception as error:
            print(
                "Image ignorée :",
                str(error),
                flush=True,
            )

    if not images:
        raise RuntimeError(
            "Aucune photo téléchargeable trouvée. "
            "Essaie un sujet simple comme sea ou nature."
        )

    (folder / "credits.json").write_text(
        json.dumps(
            credits,
            ensure_ascii=False,
            indent=2,
        ),
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
            event_type = event.get("type")

            if event_type == "audio":
                output.write(event["data"])

            elif event_type == "WordBoundary":
                words.append({
                    "start": event["offset"] / 10_000_000,
                    "duration": event["duration"] / 10_000_000,
                    "text": event["text"],
                })

    voice_path = folder / "voice.mp3"

    if not voice_path.exists() or voice_path.stat().st_size == 0:
        raise RuntimeError(
            "Le service de voix n'a renvoyé aucun son."
        )

    return words


def ass_time(seconds):
    total = max(0, round(seconds * 100))

    hours, rest = divmod(total, 360000)
    minutes, rest = divmod(rest, 6000)
    seconds, hundredths = divmod(rest, 100)

    return (
        f"{hours}:{minutes:02}:"
        f"{seconds:02}.{hundredths:02}"
    )


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
        split_words = text.split()
        step = duration / max(1, len(split_words))

        words = [
            {
                "start": index * step,
                "duration": step,
                "text": word,
            }
            for index, word in enumerate(split_words)
        ]

    lines = []

    for index, word in enumerate(words):
        start = max(0.0, float(word["start"]))

        if index + 1 < len(words):
            end = float(words[index + 1]["start"])
        else:
            end = (
                start
                + float(word["duration"])
                + 0.3
            )

        end = min(duration, end)

        if end <= start:
            continue

        clean_text = str(word["text"])
        clean_text = clean_text.replace(chr(92), "")
        clean_text = clean_text.replace("{", "")
        clean_text = clean_text.replace("}", "")

        safe = " ".join(clean_text.split())

        if not safe:
            continue

        lines.append(
            f"Dialogue: 0,{ass_time(start)},"
            f"{ass_time(end)},"
            f"Default,,0,0,0,,{safe}"
        )

    subtitle_content = header + NEWLINE.join(lines)

    (folder / "captions.ass").write_text(
        subtitle_content,
        encoding="utf-8",
    )


def generate_video(job_id, topic, text, language):
    folder = WORK / job_id

    try:
        folder.mkdir(parents=True, exist_ok=True)

        update(
            job_id,
            "Recherche et préparation des photos…",
        )

        images = find_images(topic, folder)

        update(
            job_id,
            "Création de la voix…",
        )

        try:
            words = asyncio.run(
                create_voice(text, language, folder)
            )
        except Exception as error:
            print(
                "Erreur du service vocal :",
                repr(error),
                flush=True,
            )

            raise RuntimeError(
                "La création de la voix a échoué. "
                "Réessaie avec un texte court "
                "dans la langue sélectionnée."
            ) from error

        probe_output = run_command(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "voice.mp3",
            ],
            cwd=folder,
        )

        duration = float(probe_output.strip())

        if duration <= 0:
            raise RuntimeError(
                "La durée du son est invalide."
            )

        if duration > 65:
            raise RuntimeError(
                "Texte trop long. Raccourcis-le "
                "pour rester sous 65 secondes."
            )

        create_subtitles(
            words,
            text,
            duration,
            folder,
        )

        update(
            job_id,
            "Animation des photos…",
        )

        per_scene = duration / len(images)
        frames = max(1, int(per_scene * 24) + 1)

        for index, image in enumerate(images):
            update(
                job_id,
                f"Animation de la photo "
                f"{index + 1}/{len(images)}…",
            )

            zoom_filter = (
                "zoompan="
                "z='min(zoom+0.0007,1.12)':"
                "x='iw/2-iw/zoom/2':"
                "y='ih/2-ih/zoom/2':"
                f"d={frames}:s=720x1280:fps=24"
            )

            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i", image.name,
                    "-vf", zoom_filter,
                    "-frames:v", str(frames),
                    "-an",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "26",
                    "-pix_fmt", "yuv420p",
                    "-threads", "1",
                    f"part_{index}.mp4",
                ],
                cwd=folder,
            )

        part_lines = [
            f"file 'part_{index}.mp4'"
            for index in range(len(images))
        ]

        (folder / "parts.txt").write_text(
            NEWLINE.join(part_lines) + NEWLINE,
            encoding="utf-8",
        )

        run_command(
            [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", "parts.txt",
                "-c", "copy",
                "joined.mp4",
            ],
            cwd=folder,
        )

        update(
            job_id,
            "Ajout des sous-titres et finalisation…",
        )

        run_command(
            [
                "ffmpeg",
                "-y",
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
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                "video.mp4",
            ],
            cwd=folder,
        )

        video_path = folder / "video.mp4"

        if (
            not video_path.exists()
            or video_path.stat().st_size == 0
        ):
            raise RuntimeError(
                "Le fichier vidéo final est absent."
            )

        JOBS[job_id].update({
            "status": "done",
            "message": "Vidéo prête.",
            "video": f"/files/{job_id}/video.mp4",
            "credits": f"/files/{job_id}/credits.json",
        })

    except Exception as error:
        import traceback

        traceback.print_exc()

        JOBS[job_id].update({
            "status": "error",
            "message": str(error)[:500],
        })

    finally:
        try:
            if folder.exists():
                for path in folder.iterdir():
                    if path.name not in {
                        "video.mp4",
                        "credits.json",
                    }:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
        finally:
            LOCK.release()


def valid_job_id(value):
    return (
        len(value) == 32
        and all(
            character in "0123456789abcdef"
            for character in value
        )
    )


@app.get("/")
def home():
    return PAGE


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/api/generate")
def generate():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(
            error="Requête invalide."
        ), 400

    topic = str(data.get("topic", "")).strip()
    text = str(data.get("script", "")).strip()
    language = data.get("language", "fr")

    if not topic or not text:
        return jsonify(
            error="Remplis le sujet et le texte à prononcer."
        ), 400

    if len(topic) > 120 or len(text) > 650:
        return jsonify(
            error="Le sujet ou le texte est trop long."
        ), 400

    if not isinstance(language, str) or language not in VOICES:
        return jsonify(
            error="Langue non disponible."
        ), 400

    if not LOCK.acquire(blocking=False):
        return jsonify(
            error=(
                "Une génération est déjà en cours. "
                "Réessaie après sa fin."
            )
        ), 429

    job_id = uuid.uuid4().hex

    JOBS[job_id] = {
        "status": "working",
        "message": "Démarrage…",
    }

    worker = threading.Thread(
        target=generate_video,
        args=(
            job_id,
            topic,
            text,
            language,
        ),
        daemon=True,
    )

    try:
        worker.start()
    except Exception:
        LOCK.release()
        JOBS.pop(job_id, None)

        return jsonify(
            error="Impossible de démarrer la génération."
        ), 500

    return jsonify(id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    if job_id not in JOBS:
        return jsonify(
            error=(
                "Génération introuvable. "
                "Le serveur a peut-être redémarré. "
                "Recharge la page puis réessaie."
            )
        ), 404

    response = jsonify(JOBS[job_id])
    response.headers["Cache-Control"] = "no-store"

    return response


@app.get("/files/<job_id>/<filename>")
def files(job_id, filename):
    if not valid_job_id(job_id):
        return "Introuvable", 404

    if filename not in {
        "video.mp4",
        "credits.json",
    }:
        return "Introuvable", 404

    return send_from_directory(
        WORK / job_id,
        filename,
        as_attachment=(
            request.args.get("download") == "1"
        ),
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )
