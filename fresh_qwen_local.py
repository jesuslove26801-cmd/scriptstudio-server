import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from fastapi import Body
from fastapi.responses import FileResponse, JSONResponse


QWEN_LOCAL_URL = os.environ.get("FRESH_QWEN_LOCAL_URL", "http://127.0.0.1:7860")
QWEN_LOCAL_API = os.environ.get("FRESH_QWEN_LOCAL_API", "/generate_base_17")

_OUTPUT_DIR = Path.cwd() / "outputs" / "fresh_qwen_local"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _json_error(message, status_code=500, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


def _safe_scene_id(value):
    text = str(value or "scene").strip()
    keep = []
    for ch in text:
        keep.append(ch if ch.isalnum() or ch in ("-", "_") else "_")
    result = "".join(keep).strip("_")
    return result[:48] or "scene"


def _find_audio_path(value):
    if isinstance(value, (str, os.PathLike)):
        p = Path(value)
        if p.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".ogg"} and p.exists():
            return p
        return None
    if isinstance(value, dict):
        for key in ("path", "name", "value", "file", "audio"):
            found = _find_audio_path(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _find_audio_path(item)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_audio_path(item)
            if found:
                return found
    return None


def _copy_audio_to_outputs(source_path, scene_id):
    src = Path(source_path)
    suffix = src.suffix.lower() or ".wav"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"fresh_qwen_local_{_safe_scene_id(scene_id)}_{stamp}_{uuid.uuid4().hex[:8]}{suffix}"
    target = _OUTPUT_DIR / filename
    shutil.copy2(src, target)
    return filename, target


def _new_client():
    from gradio_client import Client

    return Client(QWEN_LOCAL_URL)


def install_fresh_qwen_local_routes(app):
    @app.get("/api/fresh-qwen-local/status")
    def fresh_qwen_local_status():
        try:
            req = Request(QWEN_LOCAL_URL, headers={"User-Agent": "ScriptStudioFreshQwenLocal/1.0"})
            with urlopen(req, timeout=3) as res:
                return {"ok": True, "url": QWEN_LOCAL_URL, "statusCode": res.status}
        except Exception as exc:
            return {"ok": False, "url": QWEN_LOCAL_URL, "error": str(exc)}

    @app.post("/api/fresh-qwen-local/generate")
    def fresh_qwen_local_generate(payload: dict = Body(...)):
        text = str(payload.get("text") or "").strip()
        if not text:
            return _json_error("text is required", status_code=400)

        scene_id = payload.get("sceneId") or payload.get("scene_id") or "scene"
        voice = payload.get("voice") or "Cherry Girl"
        language = payload.get("language") or "Korean"
        seed = int(payload.get("seed") or 42)
        temperature = float(payload.get("temperature") or 0.7)
        top_p = float(payload.get("topP") or payload.get("top_p") or 0.8)
        repetition_penalty = float(payload.get("repetitionPenalty") or payload.get("repetition_penalty") or 1.1)
        max_new_tokens = int(payload.get("maxNewTokens") or payload.get("max_new_tokens") or 1200)

        try:
            client = _new_client()
            result = client.predict(
                voice,
                text,
                language,
                True,
                seed,
                temperature,
                top_p,
                repetition_penalty,
                max_new_tokens,
                api_name=QWEN_LOCAL_API,
            )
            audio_path = _find_audio_path(result)
            if not audio_path:
                return _json_error("Qwen returned no audio file", qwenResult=str(result)[:500])

            filename, target = _copy_audio_to_outputs(audio_path, scene_id)
            return {
                "ok": True,
                "sceneId": scene_id,
                "filename": filename,
                "audioUrl": f"/api/fresh-qwen-local/audio/{filename}",
                "sourcePath": str(audio_path),
                "savedPath": str(target),
            }
        except Exception as exc:
            return _json_error(str(exc))

    @app.post("/api/fresh-qwen-local/clone-generate")
    def fresh_qwen_local_clone_generate():
        return _json_error("voice clone is not wired yet", status_code=501)

    @app.get("/api/fresh-qwen-local/audio/{filename}")
    def fresh_qwen_local_audio(filename: str):
        safe_name = Path(filename).name
        path = _OUTPUT_DIR / safe_name
        if not path.exists():
            return _json_error("audio file not found", status_code=404)
        return FileResponse(path)
