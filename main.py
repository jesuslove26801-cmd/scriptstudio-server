from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from typing import List, Optional
import os
import uvicorn
import tempfile
import sys
import logging
import traceback
import json
import time
import asyncio
import base64
import httpx
import hmac
import hashlib
from renderer import render_video
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import io

# GrokBridge ?곕룞 (MakeLensAuto ?놁씠 濡쒖뺄 蹂듭궗蹂??ъ슜)
SERVER_URL = "https://web-production-11acd.up.railway.app"

# FFmpeg + fontconfig ?먮룞 ?ㅼ튂 (Railway Linux ?섍꼍)
import shutil as _shutil
if sys.platform != "win32":
    # 1. FFmpeg
    if not _shutil.which("ffmpeg"):
        try:
            import subprocess as _sp
            _sp.run([sys.executable, "-m", "pip", "install", "static-ffmpeg", "-q"], capture_output=True)
            import static_ffmpeg
            static_ffmpeg.add_paths()
        except Exception:
            pass

    # 2. fontconfig 理쒖냼 ?ㅼ젙 (?먮쭑 ASS ?꾪꽣??
    import os as _os
    if not _os.environ.get("FONTCONFIG_FILE"):
        _fc_dir = "/tmp/fc_conf"
        _os.makedirs(_fc_dir, exist_ok=True)
        _fc_cache = "/tmp/fc_cache"
        _os.makedirs(_fc_cache, exist_ok=True)
        _fc_conf = f"{_fc_dir}/fonts.conf"
        with open(_fc_conf, "w") as _f:
            _f.write(f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <dir>/usr/local/share/fonts</dir>
  <dir>/app/.fonts</dir>
  <cachedir>{_fc_cache}</cachedir>
  <config><rescan><int>30</int></rescan></config>
</fontconfig>""")
        _os.environ["FONTCONFIG_FILE"] = _fc_conf
        _os.environ["FONTCONFIG_PATH"] = _fc_dir
        # apt濡?湲곕낯 ?고듃 ?ㅼ튂 ?쒕룄
        try:
            import subprocess as _sp
            _sp.run(["apt-get", "install", "-y", "-q", "fontconfig", "fonts-dejavu-core"], capture_output=True)
            _sp.run(["fc-cache", "-f", _fc_cache], capture_output=True)
        except Exception:
            pass

_grok_bridge = None
_grok_available = False
_grok_load_error = ""
# ?꾨즺??Grok ?곸긽 紐⑸줉: {sceneNo: videoPath} ???꾨줎?몄뿏?쒓? ?대쭅?댁꽌 ?ъ뿉 ?곸슜
_grok_completed_videos: dict = {}
# ?ㅽ뙣??Grok ??紐⑸줉: {sceneNo: errorMsg}
_grok_failed_scenes: dict = {}
# ?묒뾽 ?꾩넚 ?쒓컖 (???쒓컖 ?댄썑 ?앹꽦???뚯씪留?媛먯?)
_grok_session_start_time: float = 0.0
# ??踰덊샇蹂??먮낯 ?묒뾽 ?곗씠??罹먯떆 (?ъ떎???붿껌 ???쒓났??
_grok_task_cache: dict = {}

def _try_load_grok_bridge():
    global _grok_bridge, _grok_available, _grok_load_error
    import traceback as _tb
    # PyInstaller 踰덈뱾 寃쎈줈瑜?sys.path??異붽?
    _meipass = getattr(sys, '_MEIPASS', None)
    if _meipass and _meipass not in sys.path:
        sys.path.insert(0, _meipass)
    # ?꾩옱 ?뚯씪 ?붾젆?좊━??異붽?
    _curdir = os.path.dirname(os.path.abspath(__file__))
    if _curdir not in sys.path:
        sys.path.insert(0, _curdir)
    try:
        from grok_bridge import get_bridge as _get_grok_bridge
        _grok_bridge = _get_grok_bridge(9876)
        _grok_available = True
        _grok_load_error = ""

        def _on_video_moved(data):
            scene_no = data.get('sceneNo')
            target_path = data.get('targetPath', '')
            success = data.get('success', False)
            if success and scene_no and target_path:
                _grok_completed_videos[int(scene_no)] = target_path
                print(f"[ScriptStudio] ??{scene_no} ?곸긽 ?꾨즺: {target_path}")

        _grok_bridge.on_video_moved = _on_video_moved

        def _on_task_failed(data):
            scene_no = data.get('sceneNo')
            error_msg = data.get('error', '?앹꽦 ?ㅽ뙣')
            if scene_no:
                _grok_failed_scenes[int(scene_no)] = error_msg
                print(f"[ScriptStudio] ??{scene_no} ?앹꽦 ?ㅽ뙣: {error_msg}")

        _grok_bridge.on_task_failed = _on_task_failed
        
        def _on_queue_status(data):
            global _grok_queue_status
            pass # handled inside bridge

        def _on_retry_scene_request(scene_no):
            task_info = _grok_task_cache.get(scene_no)
            if task_info:
                try:
                    import asyncio
                    loop = asyncio.get_running_loop()
                    loop.create_task(_grok_bridge.send_retry_scene_data(
                        scene_no, 
                        task_info['prompt'], 
                        task_info['imageBase64'], 
                        _grok_bridge.download_folder or ''
                    ))
                except Exception as e:
                    print(f"?ъ떎???붿껌 泥섎━ ?ㅽ뙣: {e}")
            else:
                try:
                    import asyncio
                    loop = asyncio.get_running_loop()
                    loop.create_task(_grok_bridge.send_retry_scene_error(
                        scene_no, "?먮낯 ?묒뾽 ?곗씠?곕? 李얠쓣 ???놁뒿?덈떎. 蹂??꾨줈洹몃옩?먯꽌 ?ㅼ떆 ?꾩넚?댁＜?몄슂."
                    ))
                except Exception:
                    pass

        _grok_bridge.on_retry_scene_request = _on_retry_scene_request

    except Exception as _e:
        _grok_bridge = None
        _grok_available = False
        _grok_load_error = _tb.format_exc()
        # ?먮윭瑜??뚯씪??湲곕줉 (frozen exe?먯꽌 console=False?대?濡?
        try:
            _log_path = os.path.join(os.path.expanduser("~"), "grok_bridge_error.txt")
            with open(_log_path, "w", encoding="utf-8") as _lf:
                _lf.write(_grok_load_error)
        except Exception:
            pass

_try_load_grok_bridge()

# Vertex AI ?쒕퉬??怨꾩젙 ??寃쎈줈 (?놁쑝硫?Vertex AI 鍮꾪솢?깊솕)
VERTEX_KEY_PATHS = [
    os.path.join(os.path.abspath("."), "ScriptStudio_Electron_Portable", "erudite-scholar-493007-e1-df410f3a9113.json"),
    os.path.join(os.path.abspath("."), "vertex_key.json"),
    os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.abspath("."), "ScriptStudio_Electron_Portable", "erudite-scholar-493007-e1-df410f3a9113.json"),
    os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.abspath("."), "vertex_key.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ScriptStudio_Electron_Portable", "erudite-scholar-493007-e1-df410f3a9113.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vertex_key.json"),
]
_vertex_key_path = next((p for p in VERTEX_KEY_PATHS if os.path.exists(p)), None)
_vertex_token_cache = {"token": None, "expires_at": 0}

def resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# 濡쒓퉭 ?ㅼ젙
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# ?? Kaggle ?먮룞 ?ㅼ?以꾨윭 ??
_KST = pytz.timezone("Asia/Seoul")
_KAGGLE_KERNEL_SLUG_A = os.environ.get("KAGGLE_KERNEL_SLUG_A", "qwen3-tts-t4-server")   # T4 而ㅻ꼸 (二?
_KAGGLE_KERNEL_SLUG_B = os.environ.get("KAGGLE_KERNEL_SLUG_B", "qwen3-tts-t4-server")   # T4 而ㅻ꼸 (?숈씪)

async def _startup_kaggle_if_offline():
    """Railway ?ъ떆????URL???놁쑝硫?60珥??湲???Kaggle ?먮룞 ?ъ떆??""
    await asyncio.sleep(60)
    if not _kaggle_tts_url:
        logger.info("[?ъ떆??媛먯?] Kaggle URL ?놁쓬 ???먮룞 ?ъ떆???쒕룄")
        await _auto_start_kaggle(_KAGGLE_KERNEL_SLUG_B)

async def _auto_start_kaggle(slug: str):
    logger.info(f"[?ㅼ?以꾨윭] Kaggle ?먮룞 ?쒖옉: {slug}")
    try:
        loop = asyncio.get_event_loop()
        rc, out = await loop.run_in_executor(None, lambda: _kaggle_push_sync_slug(slug))
        logger.info(f"[?ㅼ?以꾨윭] push rc={rc} out={out[:200]}")
    except Exception as e:
        logger.error(f"[?ㅼ?以꾨윭] ?ㅻ쪟: {e}")

def _kaggle_push_sync_slug(slug: str):
    import subprocess as sp
    _setup_kaggle_env()
    script = KAGGLE_SETUP_SCRIPT.replace("__GITHUB_TOKEN__", _GITHUB_TOKEN).replace("__RAILWAY_URL__", _RAILWAY_URL)
    kernel_dir = tempfile.mkdtemp()
    nb = {"cells": [{"cell_type": "code", "source": script, "metadata": {}, "outputs": [], "execution_count": None}],
          "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                       "language_info": {"name": "python", "version": "3.10.0"}}, "nbformat": 4, "nbformat_minor": 5}
    with open(os.path.join(kernel_dir, "kernel.ipynb"), "w") as f:
        json.dump(nb, f)
    meta = {"id": f"{_KAGGLE_USERNAME}/{slug}", "title": slug, "code_file": "kernel.ipynb",
            "language": "python", "kernel_type": "notebook", "is_private": True,
            "enable_gpu": True, "enable_internet": True, "dataset_sources": [], "competition_sources": [], "kernel_sources": []}
    with open(os.path.join(kernel_dir, "kernel-metadata.json"), "w") as f:
        json.dump(meta, f)
    result = sp.run(["kaggle", "kernels", "push", "-p", kernel_dir], capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout + result.stderr

_scheduler = AsyncIOScheduler(timezone=_KST)

@app.on_event("startup")
async def startup_scheduler():
    _scheduler.add_job(_auto_start_kaggle, args=[_KAGGLE_KERNEL_SLUG_A],
                       trigger=CronTrigger(hour=0, minute=0, timezone=_KST), id="kaggle_midnight")
    _scheduler.add_job(_auto_start_kaggle, args=[_KAGGLE_KERNEL_SLUG_B],
                       trigger=CronTrigger(hour=12, minute=0, timezone=_KST), id="kaggle_noon")
    _scheduler.start()
    logger.info("[?ㅼ?以꾨윭] Kaggle ?먮룞 ?쒖옉 ?깅줉: ?명듃遺갂=?먯젙, ?명듃遺갃=?뺤삤 (KST)")
    # Railway ?ъ떆????URL??鍮꾩뼱?덉쑝硫??먮룞?쇰줈 Kaggle 而ㅻ꼸 ?ъ떆??
    asyncio.create_task(_startup_kaggle_if_offline())

@app.on_event("shutdown")
async def shutdown_scheduler():
    _scheduler.shutdown()

# Qwen3-TTS 濡쒖뺄 (Gradio) ?쇱슦???깅줉
try:
    from fresh_qwen_local import install_fresh_qwen_local_routes
    install_fresh_qwen_local_routes(app)
except Exception as _fql_err:
    logger.warning(f"[fresh_qwen_local] ?쇱슦???깅줉 ?ㅽ뙣 (臾댁떆): {_fql_err}")

# CORS ?ㅼ젙
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_STYLE_PREVIEWS_DIR = os.path.join(os.path.dirname(__file__), "style-previews")
if os.path.isdir(_STYLE_PREVIEWS_DIR):
    app.mount("/style-previews", StaticFiles(directory=_STYLE_PREVIEWS_DIR), name="style-previews")

# 422 ?ㅻ쪟 ?곸꽭 濡쒓퉭 ?몃뱾??
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    safe_errors = []
    for e in exc.errors():
        safe_errors.append({
            "loc": [str(x) for x in e.get("loc", [])],
            "msg": str(e.get("msg", "")),
            "type": str(e.get("type", "")),
        })
        logger.error(f"  [422] ?꾨뱶: {e.get('loc')} | ?ㅻ쪟: {e.get('msg')} | ??? {e.get('type')}")
    return JSONResponse(
        status_code=422,
        content={"status": "error", "message": "?붿껌 ?곗씠???뺤떇 ?ㅻ쪟", "detail": safe_errors}
    )

# ?꾩뿭 ?덉쇅 ?몃뱾?????대뼡 ?덉쇅??JSON?쇰줈 諛섑솚
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    logger.error(f"[?꾩뿭?ㅻ쪟] {exc}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": str(exc), "detail": tb[-1000:]}
    )

# ?곗씠???ㅽ궎留?
class SceneData(BaseModel):
    model_config = {"extra": "ignore"}
    id: int
    script: str
    imgDataUrl: Optional[str] = None
    audioDataUrl: Optional[str] = None
    audioPath: Optional[str] = None
    dur: float
    durationSec: Optional[float] = None
    gapSec: float = 0
    subtitleTimings: Optional[list] = None
    textChunks: Optional[list] = None
    subtitlePreviewLocked: bool = False
    grokVideo: bool = False
    grokVideoPath: Optional[str] = None
    avatarUrl: Optional[str] = None
    useFade: bool = False
    zoom_type: Optional[str] = None
    motionEffect: Optional[str] = None
    transition: Optional[str] = None

class RenderRequest(BaseModel):
    model_config = {"extra": "ignore"}
    grok_download_folder: Optional[str] = None
    device_id: Optional[str] = None
    scenes: List[SceneData]
    w: int
    h: int
    fps: int
    subtitle_font: str = "留묒? 怨좊뵓"
    subtitle_color: str = "&H00FFFFFF"
    subtitle_bg: str = "box"
    subtitle_size: int = 45
    subtitle_position: str = "bottom"
    subtitle_preview_locked: bool = False
    transition_type: str = "hard"
    zoom_speed: float = 1.08
    use_zoompan: bool = True
    global_zoom_type: str = "zoom_in"
    show_subtitle: bool = True
    motion_effect: Optional[str] = None

# ?뺤쟻 ?뚯씪 ?쒕튃???꾪븳 寃쎈줈 ?ㅼ젙
@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_path = resource_path("index.html") if os.path.exists(resource_path("index.html")) else resource_path("理쒖쥌蹂?html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    from fastapi.responses import HTMLResponse as _HR
    return _HR(content=content, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

font_map_cache = {}

def load_font_map():
    if font_map_cache: return font_map_cache

    if sys.platform != "win32":
        for name in ["Arial", "DejaVu Sans", "Liberation Sans", "FreeSans", "NanumGothic"]:
            font_map_cache[name] = ""
        return font_map_cache

    import winreg
    keys_to_check = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts", os.environ.get('WINDIR', 'C:\\Windows') + '\\Fonts'),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts", os.environ.get('LOCALAPPDATA', '') + '\\Microsoft\\Windows\\Fonts')
    ]

    for root_key, sub_key, default_dir in keys_to_check:
        try:
            key = winreg.OpenKey(root_key, sub_key)
            try:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, i)
                    clean_name = name.split(" (")[0]
                    if os.path.isabs(value):
                        font_map_cache[clean_name] = value
                    else:
                        font_map_cache[clean_name] = os.path.join(default_dir, value)
            except OSError:
                pass
            finally:
                winreg.CloseKey(key)
        except OSError:
            pass
    return font_map_cache

# OAuth2 濡쒓렇???곹깭
_oauth_state: dict = {}  # flow, credentials, project_id

@app.get("/api/vertex-token")
async def get_vertex_token():
    """Vertex AI ?좏겙 諛섑솚: OAuth2 濡쒓렇???곗꽑, ?놁쑝硫??쒕퉬??怨꾩젙 ??""
    now = time.time()

    # ??OAuth2 濡쒓렇?몃맂 寃쎌슦
    creds_oauth = _oauth_state.get("credentials")
    if creds_oauth:
        if _vertex_token_cache["token"] and _vertex_token_cache["expires_at"] > now + 60:
            return {"status": "ok", "token": _vertex_token_cache["token"],
                    "project_id": _oauth_state.get("project_id", "")}
        try:
            import google.auth.transport.requests as _tr
            creds_oauth.refresh(_tr.Request())
            _vertex_token_cache["token"] = creds_oauth.token
            _vertex_token_cache["expires_at"] = now + 3600
            return {"status": "ok", "token": creds_oauth.token,
                    "project_id": _oauth_state.get("project_id", "")}
        except Exception as e:
            logger.error(f"OAuth ?좏겙 媛깆떊 ?ㅽ뙣: {e}")
            return JSONResponse(status_code=401, content={"status": "error", "message": f"OAuth ?좏겙 媛깆떊 ?ㅽ뙣: {e}"})

    # ???쒕퉬??怨꾩젙 ???뚯씪
    if not _vertex_key_path:
        return JSONResponse(status_code=404, content={"status": "error", "message": "濡쒓렇?몄씠 ?꾩슂?⑸땲??(Google 濡쒓렇??踰꾪듉 ?대┃)"})

    if _vertex_token_cache["token"] and _vertex_token_cache["expires_at"] > now + 60:
        return {"status": "ok", "token": _vertex_token_cache["token"], "project_id": _vertex_project_id}

    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests

        creds = service_account.Credentials.from_service_account_file(
            _vertex_key_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        req = google.auth.transport.requests.Request()
        creds.refresh(req)

        _vertex_token_cache["token"] = creds.token
        _vertex_token_cache["expires_at"] = now + 3600

        with open(_vertex_key_path, "r") as f:
            key_data = json.load(f)
        project_id = key_data.get("project_id", "")

        return {"status": "ok", "token": creds.token, "project_id": project_id}
    except Exception as e:
        logger.error(f"Vertex AI ?좏겙 諛쒓툒 ?ㅽ뙣: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ?섍꼍蹂??VERTEX_KEY_JSON ?먯꽌 ??濡쒕뱶 (Railway ?ъ떆???꾩뿉???좎?)
_VERTEX_KEY_ENV = os.environ.get("VERTEX_KEY_JSON", "")
if _VERTEX_KEY_ENV and not _vertex_key_path:
    try:
        import base64 as _b64
        _key_raw = _b64.b64decode(_VERTEX_KEY_ENV).decode() if not _VERTEX_KEY_ENV.strip().startswith("{") else _VERTEX_KEY_ENV
        _key_data = json.loads(_key_raw)
        _env_key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vertex_key.json")
        with open(_env_key_path, "w") as _f:
            json.dump(_key_data, _f)
        _vertex_key_path = _env_key_path
    except Exception as _e:
        pass

# project_id 罹먯떆 (?쒕퉬??怨꾩젙??
_vertex_project_id = ""
try:
    if _vertex_key_path:
        with open(_vertex_key_path, "r") as _f:
            _vertex_project_id = json.load(_f).get("project_id", "")
except Exception:
    pass

@app.post("/api/vertex-key-upload")
async def vertex_key_upload(file: UploadFile = File(...)):
    global _vertex_key_path, _vertex_project_id, _vertex_token_cache
    try:
        contents = await file.read()
        key_data = json.loads(contents)
        if key_data.get("type") != "service_account":
            return JSONResponse(status_code=400, content={"status": "error", "message": "?쒕퉬??怨꾩젙 ???뚯씪???꾨떃?덈떎"})
        project_id = key_data.get("project_id", "")
        save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vertex_key.json")
        with open(save_path, "wb") as f:
            f.write(contents)
        _vertex_key_path = save_path
        _vertex_project_id = project_id
        _vertex_token_cache = {"token": None, "expires_at": 0}
        return {"status": "ok", "project_id": project_id}
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "?좏슚??JSON ?뚯씪???꾨떃?덈떎"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ?? Google OAuth2 濡쒓렇???붾뱶?ъ씤????

class GoogleAuthStartReq(BaseModel):
    client_id: str
    client_secret: str
    project_id: str

@app.post("/api/auth/google/start")
async def google_auth_start(req: GoogleAuthStartReq):
    """OAuth2 ?몄쬆 URL ?앹꽦 諛?釉뚮씪?곗? ?ㅽ뵂"""
    import webbrowser
    try:
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": req.client_id,
                "client_secret": req.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:8000/api/auth/google/callback"]
            }
        }
        flow = Flow.from_client_config(
            client_config,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            redirect_uri="http://localhost:8000/api/auth/google/callback"
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )
        _oauth_state["flow"] = flow
        _oauth_state["state"] = state
        _oauth_state["project_id"] = req.project_id
        _oauth_state["credentials"] = None
        _vertex_token_cache["token"] = None
        _vertex_token_cache["expires_at"] = 0

        webbrowser.open(auth_url)
        return {"status": "ok", "auth_url": auth_url}
    except Exception as e:
        logger.error(f"OAuth ?쒖옉 ?ㅻ쪟: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/auth/google/callback")
async def google_auth_callback(code: str = "", error: str = "", state: str = ""):
    """OAuth2 肄쒕갚 ???몄쬆 肄붾뱶 援먰솚"""
    if error:
        return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:40px'><h2>???몄쬆 痍⑥냼??/h2><p>{error}</p><p>??李쎌쓣 ?レ쑝?몄슂.</p></body></html>")
    if not code:
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'><h2>??肄붾뱶 ?놁쓬</h2><p>??李쎌쓣 ?レ쑝?몄슂.</p></body></html>")

    flow = _oauth_state.get("flow")
    if not flow:
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'><h2>???몄뀡 留뚮즺</h2><p>?ㅼ떆 濡쒓렇?명빐二쇱꽭??</p></body></html>")

    try:
        import os as _os
        _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        flow.fetch_token(code=code)
        creds = flow.credentials

        _oauth_state["credentials"] = creds
        _vertex_token_cache["token"] = creds.token
        _vertex_token_cache["expires_at"] = time.time() + 3600

        logger.info("Google OAuth2 濡쒓렇???깃났")
        return HTMLResponse("""
<html><head><meta charset='utf-8'></head>
<body style='font-family:sans-serif;padding:40px;text-align:center;background:#0d1117;color:#e6edf3'>
  <h2 style='color:#36d68a'>??Google 濡쒓렇???깃났!</h2>
  <p>??李쎌쓣 ?リ퀬 ScriptStudio濡??뚯븘媛?몄슂.</p>
  <script>setTimeout(()=>window.close(),2000);</script>
</body></html>""")
    except Exception as e:
        logger.error(f"OAuth 肄쒕갚 ?ㅻ쪟: {e}")
        return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:40px'><h2>???몄쬆 ?ㅻ쪟</h2><p>{e}</p></body></html>")

@app.get("/api/auth/google/status")
async def google_auth_status():
    """濡쒓렇???곹깭 議고쉶"""
    creds = _oauth_state.get("credentials")
    if creds and _vertex_token_cache.get("token"):
        return {"status": "ok", "logged_in": True, "project_id": _oauth_state.get("project_id", "")}
    # ?쒕퉬??怨꾩젙 ?ㅺ? ?덉쑝硫?洹멸쾬??'?곌껐???쇰줈 媛꾩＜
    if _vertex_key_path:
        return {"status": "ok", "logged_in": True, "project_id": _vertex_project_id, "via": "service_account"}
    return {"status": "ok", "logged_in": False}

@app.post("/api/auth/google/logout")
async def google_auth_logout():
    """濡쒓렇?꾩썐"""
    _oauth_state.clear()
    _vertex_token_cache["token"] = None
    _vertex_token_cache["expires_at"] = 0
    return {"status": "ok"}

@app.get("/api/fonts")
async def api_fonts():
    fmap = load_font_map()
    fonts_list = sorted(list(fmap.keys()))
    if not fonts_list:
        fonts_list = ["留묒? 怨좊뵓", "援대┝", "?뗭?", "沅곸꽌", "Arial"]
    return {"status": "success", "fonts": fonts_list}

@app.get("/api/font_file/{font_name}")
async def get_font_file(font_name: str):
    fmap = load_font_map()
    if font_name in fmap:
        path = fmap[font_name]
        if os.path.exists(path):
            return FileResponse(path)
    return {"error": "Font file not found"}

@app.get("/api/status")
async def api_status():
    import shutil
    ffmpeg_path = shutil.which("ffmpeg")
    return {"status": "ok", "platform": sys.platform, "ffmpeg": ffmpeg_path or "NOT FOUND"}

# Kaggle TTS ?쒕쾭 URL 釉뚮줈而?
_URL_CACHE_FILE = "/tmp/kaggle_tts_url.txt"

def _load_cached_url() -> str:
    try:
        if os.path.exists(_URL_CACHE_FILE):
            return open(_URL_CACHE_FILE).read().strip()
    except Exception:
        pass
    return ""

def _save_cached_url(url: str):
    try:
        with open(_URL_CACHE_FILE, "w") as f:
            f.write(url)
    except Exception:
        pass

_kaggle_tts_url: str = ""
_kaggle_url_registered_at: float = 0.0
_KAGGLE_SECRET = os.environ.get("KAGGLE_SECRET", "")

# ?? Edge TTS ??????????????????????????????????????????????????????????????????
EDGE_VOICES = {
    "SunHi":  "ko-KR-SunHiNeural",   # ?ъ꽦 ?쒓뎅??
    "InJoon": "ko-KR-InJoonNeural",  # ?⑥꽦 ?쒓뎅??
    "HyunSu": "ko-KR-HyunsuNeural", # ?⑥꽦 ?쒓뎅??
    "Aria":   "en-US-AriaNeural",    # ?ъ꽦 ?곸뼱
    "Guy":    "en-US-GuyNeural",     # ?⑥꽦 ?곸뼱
}

@app.post("/api/edge-tts")
async def edge_tts_generate(req: Request):
    try:
        import edge_tts
        body = await req.json()
        text = body.get("text", "").strip()
        voice_name = body.get("voice", "SunHi")
        speed = float(body.get("speed", 1.0))
        if not text:
            return JSONResponse({"error": "text required"}, status_code=400)
        voice_id = EDGE_VOICES.get(voice_name, "ko-KR-SunHiNeural")
        rate = f"+{int((speed-1)*100)}%" if speed >= 1 else f"{int((speed-1)*100)}%"
        communicate = edge_tts.Communicate(text, voice_id, rate=rate)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        buf.seek(0)
        from fastapi.responses import StreamingResponse
        return StreamingResponse(buf, media_type="audio/mpeg")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/edge-tts/voices")
async def edge_tts_voices():
    return {"voices": [{"id": k, "description": v} for k, v in EDGE_VOICES.items()]}

# 蹂댁씠???꾨줈????μ냼 (Railway ?ъ떆???꾧퉴吏 ?좎?)
_voice_profiles: dict = {}  # name ??{audio_b64, language, ref_text}

@app.post("/api/voice-profile/save")
async def save_voice_profile(req: Request):
    form = await req.form()
    name = (form.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"status": "error", "message": "name required"})
    audio_file = form.get("ref_audio")
    if not audio_file:
        return JSONResponse(status_code=400, content={"status": "error", "message": "ref_audio required"})
    audio_bytes = await audio_file.read()
    _voice_profiles[name] = {
        "name": name,
        "language": form.get("language") or "Korean",
        "ref_text": form.get("ref_text") or "",
        "audio_b64": base64.b64encode(audio_bytes).decode()
    }
    logger.info(f"蹂댁씠???꾨줈????? {name}")
    return {"status": "ok", "name": name}

@app.get("/api/voice-profile/list")
async def list_voice_profiles():
    return {"status": "ok", "profiles": [{"name": v["name"], "language": v["language"]} for v in _voice_profiles.values()]}

@app.get("/api/voice-profile/get/{name}")
async def get_voice_profile(name: str):
    p = _voice_profiles.get(name)
    if not p:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Profile not found"})
    return {"status": "ok", **p}

@app.delete("/api/voice-profile/delete/{name}")
async def delete_voice_profile(name: str):
    if name in _voice_profiles:
        del _voice_profiles[name]
        return {"status": "ok"}
    return JSONResponse(status_code=404, content={"status": "error"})

@app.post("/api/set-qwen-url")
async def set_qwen_url(req: Request):
    data = await req.json()
    secret = data.get("secret", "")
    if _KAGGLE_SECRET and secret != _KAGGLE_SECRET:
        return JSONResponse(status_code=401, content={"status": "error"})
    global _kaggle_tts_url, _kaggle_url_registered_at
    new_url = data.get("url", "").rstrip("/")

    _kaggle_tts_url = new_url
    _kaggle_url_registered_at = time.time()
    _save_cached_url(_kaggle_tts_url)
    logger.info(f"Kaggle TTS URL ?깅줉: {_kaggle_tts_url}")
    return {"status": "ok", "url": _kaggle_tts_url}

@app.get("/api/qwen-url")
async def get_qwen_url():
    if not _kaggle_tts_url:
        return JSONResponse(status_code=503, content={"status": "offline", "message": "TTS ?쒕쾭媛 ?ㅽ봽?쇱씤?낅땲?? Kaggle ?명듃遺곸쓣 ?ㅽ뻾?섏꽭??"})
    return {"status": "ok", "url": _kaggle_tts_url}

_KAGGLE_USERNAME = os.environ.get("KAGGLE_USERNAME", "junryong")
_KAGGLE_KEY = os.environ.get("KAGGLE_KEY", "7a6425f7c4e48f1dc705fb462b94dc20")
_KAGGLE_KERNEL_SLUG = os.environ.get("KAGGLE_KERNEL_SLUG", "qwen3-tts-t4-server")
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "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")
_RAILWAY_URL = os.environ.get("RAILWAY_URL", "https://web-production-11acd.up.railway.app")

KAGGLE_SETUP_SCRIPT = '''\
import subprocess, sys, os, time, re, requests, importlib.util

GITHUB_TOKEN = "__GITHUB_TOKEN__"
RAILWAY_URL = "__RAILWAY_URL__"

# 1. git clone (shallow)
subprocess.run(["rm", "-rf", "/kaggle/working/tts"])
subprocess.run(["git", "clone", "--depth", "1",
    f"https://jesuslove26801-cmd:{GITHUB_TOKEN}@github.com/jesuslove26801-cmd/qwen3-tts-runpod",
    "/kaggle/working/tts"])

# 2. transformers 踰꾩쟾 ?뺤씤 ???꾩슂???뚮쭔 ?ъ꽕移?
try:
    import transformers as _tr
    assert _tr.__version__ == "4.57.3"
    print(f"transformers {_tr.__version__} ?대? ?ㅼ튂????skip")
except (ImportError, AssertionError):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "transformers==4.57.3"])

# 3. ?녿뒗 ?⑦궎吏留??ㅼ튂
_need = {p: n for p, n in {
    "fastapi": "fastapi", "uvicorn": "uvicorn", "python-multipart": "multipart",
    "soundfile": "soundfile", "librosa": "librosa", "einops": "einops",
    "pydub": "pydub", "scipy": "scipy", "sox": "sox", "onnxruntime": "onnxruntime",
    "pyyaml": "yaml", "accelerate": "accelerate"
}.items() if importlib.util.find_spec(n) is None}
if _need:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + list(_need.keys()))

# 4. qwen-tts ??긽 ?ъ꽕移?
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "-e", "/kaggle/working/tts"])

if not os.path.exists("/usr/local/bin/cloudflared"):
    subprocess.run(["wget", "-q",
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        "-O", "/usr/local/bin/cloudflared"])
    subprocess.run(["chmod", "+x", "/usr/local/bin/cloudflared"])

subprocess.run(["pkill", "-f", "uvicorn"], capture_output=True)
time.sleep(2)

# T4 理쒖쟻?? config.yaml ?앹꽦 (CUDA Graphs + torch.compile)
import pathlib
_cfg_dir = pathlib.Path.home() / "qwen3-tts"
_cfg_dir.mkdir(exist_ok=True)
(_cfg_dir / "config.yaml").write_text("""default_model: 1.7B-CustomVoice
models:
  1.7B-CustomVoice:
    hf_id: Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
    type: customvoice
  1.7B-Base:
    hf_id: Qwen/Qwen3-TTS-12Hz-1.7B-Base
    type: base
optimization:
  attention: sdpa
  use_compile: true
  compile_mode: reduce-overhead
  use_cuda_graphs: true
  use_fast_codebook: true
  compile_codebook_predictor: true
""")

env = {**os.environ,
       "TTS_BACKEND": "optimized",
       "TTS_WARMUP_ON_START": "true",
       "TTS_MODEL_NAME": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"}
log = open("/kaggle/working/server.log", "w")
subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app",
    "--host", "0.0.0.0", "--port", "8880"],
    cwd="/kaggle/working/tts", env=env, stdout=log, stderr=log)

cf_log = open("/kaggle/working/cf.log", "w")
subprocess.Popen(["/usr/local/bin/cloudflared", "tunnel", "--url", "http://localhost:8880"],
    stderr=cf_log, stdout=subprocess.DEVNULL)

# 5. cloudflare ?대쭅 (90珥?怨좎젙 ??理쒕? 2遺??대쭅)
tunnel_url = None
for _ in range(40):
    time.sleep(3)
    with open("/kaggle/working/cf.log") as f:
        m = re.search(r"https://\S+\.trycloudflare\.com", f.read())
        if m:
            tunnel_url = m.group()
            break
print("Tunnel URL:", tunnel_url)

if tunnel_url:
    r = requests.post(f"{RAILWAY_URL}/api/set-qwen-url",
        json={"url": tunnel_url, "secret": ""})
    print("Railway ?깅줉:", r.status_code, r.json())
    # 紐⑤뜽 ?먮룞 濡쒕뵫 ?湲?(理쒕? 10遺?
    print("紐⑤뜽 ?먮룞 濡쒕뵫 ?湲?以?.. /health ?대쭅")
    for _w in range(200):
        time.sleep(3)
        try:
            hr = requests.get(f"{tunnel_url}/health", timeout=10)
            if hr.status_code == 200:
                info = hr.json()
                if info.get("backend", {}).get("ready", False):
                    print(f"??紐⑤뜽 濡쒕뵫 ?꾨즺! ({_w * 3}珥?")
                    break
                else:
                    if _w % 10 == 0:
                        print(f"紐⑤뜽 濡쒕뵫 以?.. ({_w * 3}珥?寃쎄낵)")
        except Exception as e:
            if _w % 10 == 0:
                print(f"?쒕쾭 ?湲곗쨷: {e}")
else:
    print("?곕꼸 URL??李얠? 紐삵뻽?듬땲??")

# 蹂댁씠???꾨줈???먮룞 ?숆린??(Railway ??Kaggle)
print("蹂댁씠???꾨줈???숆린??以?..")
try:
    import base64, io
    pl_res = requests.get(f"{RAILWAY_URL}/api/voice-profile/list", timeout=10)
    profiles = pl_res.json().get("profiles", [])
    for p in profiles:
        pname = p["name"]
        try:
            det = requests.get(f"{RAILWAY_URL}/api/voice-profile/get/{pname}", timeout=10).json()
            audio_bytes = base64.b64decode(det["audio_b64"])
            files = {"ref_audio": (f"{pname}.wav", io.BytesIO(audio_bytes), "audio/wav")}
            data = {"name": pname, "language": det.get("language","Korean"),
                    "ref_text": det.get("ref_text",""),
                    "x_vector_only_mode": "false" if det.get("ref_text") else "true"}
            reg = requests.post(f"http://localhost:8880/v1/voice-library/profiles",
                                files=files, data=data, timeout=30)
            print(f"  {pname} ?깅줉: {reg.status_code}")
        except Exception as pe:
            print(f"  {pname} ?ㅽ뙣: {pe}")
    if not profiles:
        print("  ??λ맂 ?꾨줈???놁쓬")
except Exception as e:
    print(f"?꾨줈???숆린???ㅽ뙣: {e}")

# 而ㅻ꼸 ?댁븘?덈뒗 ?숈븞 ?쒕쾭 ?좎? + 5遺꾨쭏??URL ?щ벑濡?(Railway ?ъ떆?????
print("?쒕쾭 ?좎? 以?.. (理쒕? 9?쒓컙)")
_keep_tick = 0
while True:
    time.sleep(60)
    _keep_tick += 1
    if tunnel_url and _keep_tick % 5 == 0:
        try:
            requests.post(f"{RAILWAY_URL}/api/set-qwen-url",
                json={"url": tunnel_url, "secret": ""}, timeout=10)
        except Exception:
            pass
'''

def _setup_kaggle_env():
    kaggle_dir = os.path.expanduser("~/.kaggle")
    os.makedirs(kaggle_dir, exist_ok=True)
    kaggle_json = os.path.join(kaggle_dir, "kaggle.json")
    with open(kaggle_json, "w") as f:
        json.dump({"username": _KAGGLE_USERNAME, "key": _KAGGLE_KEY}, f)
    os.chmod(kaggle_json, 0o600)

_kaggle_start_time: float = 0.0

def _kaggle_push_sync(script: str):
    import tempfile, subprocess as sp
    _setup_kaggle_env()
    notebook = {
        "cells": [{"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                   "source": [line + "\n" for line in script.strip().split("\n")]}],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python", "version": "3.10.0"}},
        "nbformat": 4, "nbformat_minor": 4
    }
    meta = {
        "id": f"{_KAGGLE_USERNAME}/{_KAGGLE_KERNEL_SLUG}",
        "title": "Qwen3 TTS Server",
        "code_file": "kernel.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True
    }
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "kernel.ipynb"), "w") as f:
            json.dump(notebook, f)
        with open(os.path.join(tmp, "kernel-metadata.json"), "w") as f:
            json.dump(meta, f)
        # ?붾쾭洹? ?ㅼ젣 ?곗뿬吏?硫뷀??곗씠???뺤씤
        with open(os.path.join(tmp, "kernel-metadata.json")) as dbg:
            logger.info(f"metadata content: {dbg.read()}")
        result = sp.run(["kaggle", "kernels", "push", "-p", tmp],
                        capture_output=True, text=True, timeout=60)
        logger.info(f"kaggle push stdout: {result.stdout[:200]} stderr: {result.stderr[:200]}")
        return result.returncode, result.stdout + result.stderr

def _kaggle_status_sync():
    import subprocess as sp
    _setup_kaggle_env()
    result = sp.run(["kaggle", "kernels", "status", f"{_KAGGLE_USERNAME}/{_KAGGLE_KERNEL_SLUG}"],
                    capture_output=True, text=True, timeout=30)
    return result.stdout + result.stderr

@app.post("/api/start-kaggle")
async def start_kaggle_server(req: Request):
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    master_email = os.environ.get("MASTER_EMAIL", "master@gmail.com")
    if data.get("email", "").lower() != master_email.lower():
        return JSONResponse(status_code=403, content={"status": "error", "message": "沅뚰븳 ?놁쓬"})
    global _kaggle_start_time
    if not _GITHUB_TOKEN:
        return JSONResponse(status_code=503, content={"status": "error", "message": "GITHUB_TOKEN 誘몄꽕??})

    _kaggle_start_time = time.time()
    script = KAGGLE_SETUP_SCRIPT.replace("__GITHUB_TOKEN__", _GITHUB_TOKEN).replace("__RAILWAY_URL__", _RAILWAY_URL)

    try:
        rc, out = await asyncio.get_event_loop().run_in_executor(None, _kaggle_push_sync, script)
        logger.info(f"kaggle push rc={rc} out={out[:200]}")
        if rc == 0:
            return {"status": "ok", "message": "Kaggle ?명듃遺??ㅽ뻾 ?쒖옉??}
        else:
            return JSONResponse(status_code=500, content={"status": "error", "message": f"kaggle push ?ㅽ뙣: {out[:300]}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/kaggle-status")
async def get_kaggle_status():
    # push ?댄썑???깅줉??URL留?ready濡?諛섑솚 (?댁쟾 ?몄뀡 URL 臾댁떆)
    if _kaggle_tts_url and _kaggle_url_registered_at >= _kaggle_start_time:
        return {"status": "ready", "url": _kaggle_tts_url, "message": "???쒕쾭 以鍮??꾨즺!"}
    try:
        out = await asyncio.get_event_loop().run_in_executor(None, _kaggle_status_sync)
        logger.info(f"kaggle status: {out[:100]}")
        if "running" in out.lower():
            return {"status": "running", "message": "?봽 ?쒕쾭 ?쒖옉 以?.. (??2~3遺??뚯슂)"}
        elif "queued" in out.lower():
            return {"status": "queued", "message": "??GPU ?좊떦 ?湲?以?.."}
        elif "complete" in out.lower():
            return {"status": "complete", "message": "?꾨즺 (?곕꼸 ?깅줉 ?湲?以?..)"}
        elif "error" in out.lower():
            return {"status": "error", "message": f"???ㅻ쪟: {out[:100]}"}
    except Exception as e:
        logger.warning(f"Kaggle ?곹깭 議고쉶 ?ㅽ뙣: {e}")
    return {"status": "idle", "message": ""}

@app.post("/api/stop-kaggle")
async def stop_kaggle_server(req: Request):
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    master_email = os.environ.get("MASTER_EMAIL", "master@gmail.com")
    if data.get("email", "").lower() != master_email.lower():
        return JSONResponse(status_code=403, content={"status": "error", "message": "沅뚰븳 ?놁쓬"})
    global _kaggle_tts_url
    try:
        import subprocess as sp
        _setup_kaggle_env()
        sp.run(["kaggle", "kernels", "cancel", f"{_KAGGLE_USERNAME}/{_KAGGLE_KERNEL_SLUG}"],
               capture_output=True, timeout=30)
        _kaggle_tts_url = ""
        return {"status": "ok", "message": "?쒕쾭 以묒? ?붿껌??}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

class EmailCheckReq(BaseModel):
    email: str
    password: str

USER_DATA_FILE = "outputs/user_data.json"
USER_DATA_TTL = 7 * 24 * 3600  # 1二쇱씪

def _get_secret():
    return os.environ.get("SECRET_KEY", "ss-secret-2026-xK9")

def _make_token(email: str) -> str:
    return hmac.new(_get_secret().encode(), email.lower().encode(), hashlib.sha256).hexdigest()

def _verify_token(email: str, token: str) -> bool:
    try:
        return hmac.compare_digest(_make_token(email.lower()), token)
    except Exception:
        return False

def _load_all_user_data() -> dict:
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _persist_all_user_data(data: dict):
    os.makedirs("outputs", exist_ok=True)
    with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

@app.post("/api/check-access")
async def check_access(req: EmailCheckReq):
    # USERS ?뺤떇: "email1:pass1,email2:pass2" (媛쒕퀎 鍮꾨?踰덊샇)
    users_env = os.environ.get("USERS", "")
    if users_env:
        user_map = {}
        for entry in users_env.split(","):
            entry = entry.strip()
            if ":" in entry:
                e, p = entry.split(":", 1)
                user_map[e.strip().lower()] = p.strip()
        if not user_map:
            return JSONResponse(status_code=500, content={"status": "error", "message": "?쒕쾭 ?ㅼ젙 ?ㅻ쪟"})
        stored = user_map.get(req.email.strip().lower())
        if stored and req.password == stored:
            return {"status": "ok", "token": _make_token(req.email.strip().lower())}
        return JSONResponse(status_code=401, content={"status": "error", "message": "?대찓???먮뒗 鍮꾨?踰덊샇媛 ?щ컮瑜댁? ?딆뒿?덈떎"})
    # ?섏쐞 ?명솚: ALLOWED_EMAILS + ACCESS_PASSWORD
    allowed = os.environ.get("ALLOWED_EMAILS", "")
    password = os.environ.get("ACCESS_PASSWORD", "")
    allowed_list = [e.strip().lower() for e in allowed.split(",") if e.strip()]
    if not allowed_list or not password:
        return JSONResponse(status_code=500, content={"status": "error", "message": "?쒕쾭 ?ㅼ젙 ?ㅻ쪟"})
    if req.email.strip().lower() in allowed_list and req.password == password:
        return {"status": "ok", "token": _make_token(req.email.strip().lower())}
    return JSONResponse(status_code=401, content={"status": "error", "message": "?대찓???먮뒗 鍮꾨?踰덊샇媛 ?щ컮瑜댁? ?딆뒿?덈떎"})

class UserSaveReq(BaseModel):
    email: str
    token: str
    data: dict

@app.post("/api/user/save")
async def user_save(req: UserSaveReq):
    if not _verify_token(req.email, req.token):
        return JSONResponse(status_code=401, content={"status": "error", "message": "?몄쬆 ?ㅽ뙣"})
    all_data = _load_all_user_data()
    now = time.time()
    all_data[req.email.strip().lower()] = {"saved_at": now, "data": req.data}
    # 留뚮즺???곗씠???뺣━
    all_data = {k: v for k, v in all_data.items() if now - v.get("saved_at", 0) < USER_DATA_TTL}
    _persist_all_user_data(all_data)
    return {"status": "ok"}

@app.get("/api/user/load")
async def user_load(email: str, token: str):
    if not _verify_token(email, token):
        return JSONResponse(status_code=401, content={"status": "error", "message": "?몄쬆 ?ㅽ뙣"})
    all_data = _load_all_user_data()
    entry = all_data.get(email.strip().lower())
    if not entry:
        return {"status": "not_found"}
    if time.time() - entry.get("saved_at", 0) > USER_DATA_TTL:
        return {"status": "expired"}
    days_left = round((USER_DATA_TTL - (time.time() - entry["saved_at"])) / 86400, 1)
    return {"status": "ok", "data": entry["data"], "saved_at": entry["saved_at"], "days_left": days_left}

@app.get("/api/progress")
async def api_progress():
    from renderer import render_progress
    return render_progress

@app.post("/api/render")
async def api_render(req: RenderRequest):
    logger.info(f"?뚮뜑留??붿껌: {len(req.scenes)}?λ㈃ | {req.w}x{req.h} @ {req.fps}fps | ?고듃={req.subtitle_font} | ?몃옖吏??{req.transition_type}")
    try:
        # Grok ?숈쁺???ъ뿉 ?ㅼ젣 ?뚯씪 寃쎈줈 二쇱엯
        for i, scene in enumerate(req.scenes):
            if scene.grokVideo and not scene.grokVideoPath:
                import re as _re
                m = _re.search(r'/api/grok/video/(\d+)', scene.imgDataUrl or '')
                if m:
                    sno = int(m.group(1))
                    fpath = _grok_completed_videos.get(sno)
                    
                    if not fpath and req.grok_download_folder:
                        test_path = os.path.join(req.grok_download_folder, f"scene_{sno}_grok.mp4")
                        if os.path.exists(test_path):
                            fpath = test_path
                            
                    if fpath and os.path.exists(fpath):
                        scene.grokVideoPath = fpath
        output_file = await render_video(req)
        logger.info(f"?뚮뜑留??꾨즺: {output_file}")
        return {"status": "success", "file": output_file}
    except Exception as e:
        logger.error(f"?뚮뜑留??ㅽ뙣: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

# ?? Companion Task ??μ냼 (硫붾え由? ????????????????????????????????????
_companion_tasks: dict = {}  # task_id ??{zip_url, device_id, status, created_at}

@app.post("/api/preview-subtitles")
async def api_preview_subtitles(req: RenderRequest):
    """?먮쭑 遺꾪븷 誘몃━蹂닿린 ???щ퀎 script瑜?泥?겕濡?遺꾪븷??諛섑솚"""
    try:
        from renderer import build_scene_subtitle_preview
        previews = []
        for sc in req.scenes:
            chunks = build_scene_subtitle_preview(sc, req, req.h or 1920)
            if not chunks:
                chunks = [(sc.script or '').strip()] if (sc.script or '').strip() else ['']
            previews.append({"lines": chunks})
        return {"status": "success", "previews": previews}
    except Exception as e:
        logger.error(f"?먮쭑 遺꾪븷 ?ㅽ뙣: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/export-capcut")
async def api_export_capcut(req: RenderRequest):
    """?뱀뿉??CapCut ?대낫?닿린 ?붿껌 ??ZIP ?앹꽦 ??companion task ?깅줉"""
    try:
        device_id = req.device_id or ""
        from renderer import export_capcut_project
        result = export_capcut_project(req, return_zip=True)
        if result.get("status") != "success":
            return JSONResponse(status_code=500, content={"status": "error", "message": "CapCut ?꾨줈?앺듃 ?앹꽦 ?ㅽ뙣"})

        zip_name  = result.get("zip_file")
        zip_url   = f"{SERVER_URL}/download-audio/{zip_name}"
        task_id   = f"cc_{int(time.time())}_{zip_name[:8]}"

        _companion_tasks[task_id] = {
            "task_id":    task_id,
            "zip_url":    zip_url,
            "device_id":  device_id or "",
            "status":     "pending",
            "created_at": time.time(),
            "draft_name": result["draft_name"],
            "grok_folder": req.grok_download_folder or "",
        }
        return {"status": "success", "task_id": task_id, "draft_name": result["draft_name"]}
    except Exception as e:
        logger.error(f"CapCut ?대낫?닿린 ?ㅽ뙣: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/companion/task/poll")
async def companion_poll(device_id: str = ""):
    """Companion ?깆씠 3珥덈쭏???몄텧 ???먯떊??device_id???대떦?섎뒗 pending ?묒뾽 諛섑솚"""
    now = time.time()
    for tid, task in list(_companion_tasks.items()):
        if task["status"] == "pending" and (not device_id or task["device_id"] == device_id):
            if now - task["created_at"] > 300:  # 5遺?吏???묒뾽 ?먮룞 ??젣
                del _companion_tasks[tid]
                continue
            task["status"] = "sent"
            return task
    return {"task_id": None}

@app.post("/api/companion/task/{task_id}/complete")
async def companion_complete(task_id: str, req: Request):
    """Companion ?깆씠 ?묒뾽 ?꾨즺/?ㅽ뙣 蹂닿퀬"""
    body = await req.json()
    if task_id in _companion_tasks:
        _companion_tasks[task_id]["status"] = body.get("status", "done")
    return {"ok": True}

@app.get("/api/companion/task/{task_id}/status")
async def companion_task_status(task_id: str):
    """???꾨줎?몄뿏?쒓? ?묒뾽 ?꾨즺 ?щ? ?대쭅"""
    task = _companion_tasks.get(task_id)
    if not task:
        return {"status": "not_found"}
    return {"status": task["status"], "draft_name": task.get("draft_name")}

# ?? Google Flow Task ??μ냼 (硫붾え由? ?????????????????????????????????
_flow_tasks: dict = {}  # task_id ??{mode, prompt, image_url, status, result_url, created_at}
_flow_last_poll: float = 0.0  # ?뚯빱 留덉?留?poll ?쒓컖
_flow_login_requested: bool = False  # ?щ줈洹몄씤 ?붿껌 ?뚮옒洹?

@app.post("/api/flow/task")
async def flow_create_task(req: Request):
    """???꾨줎?몄뿏?쒓? Flow ?묒뾽 ?깅줉 (text2img ?먮뒗 img2video)"""
    body = await req.json()
    mode = body.get("mode", "text2img")  # "text2img" | "img2video" | "text2img_video"
    if mode not in ("text2img", "img2video", "text2img_video"):
        return JSONResponse(status_code=400, content={"status": "error", "message": "mode??text2img, img2video, text2img_video"})

    task_id = f"flow_{int(time.time())}_{mode[:3]}"
    _flow_tasks[task_id] = {
        "task_id":    task_id,
        "mode":       mode,
        "prompt":     body.get("prompt", ""),
        "image_url":  body.get("image_url", ""),
        "image_data": body.get("image_data", ""),  # base64 吏곸젒 (Railway ?뚯씪?쒖뒪???고쉶)
        "delay":      int(body.get("delay", 10)),
        "status":     "pending",
        "result_url": "",
        "created_at": time.time(),
    }
    return {"status": "ok", "task_id": task_id}

@app.get("/api/companion/latest")
async def companion_latest():
    ver = "1.3.16"
    return {
        "version": ver,
        "download_url": f"https://scriptstudio-web.pages.dev/ScriptStudio_Companion_Setup_v{ver}.bat"
    }

@app.get("/api/flow/worker/status")
async def flow_worker_status():
    """?꾨줎?몄뿏?쒖뿉??Flow ?뚯빱 ?곌껐 ?щ? ?뺤씤"""
    connected = (time.time() - _flow_last_poll) < 30
    return {"connected": connected}

@app.post("/api/flow/worker/request-login")
async def flow_request_login():
    """?꾨줎?몄뿏?쒖뿉??援ш? ?щ줈洹몄씤 ?붿껌 ???ㅼ쓬 poll?먯꽌 ?뚯빱媛 媛먯?"""
    global _flow_login_requested
    _flow_login_requested = True
    return {"ok": True}

@app.get("/api/flow/task/poll")
async def flow_poll():
    """濡쒖뺄 Flow ?뚯빱媛 二쇨린?곸쑝濡??몄텧 ??pending ?묒뾽 諛섑솚"""
    global _flow_last_poll, _flow_login_requested
    _flow_last_poll = time.time()
    if _flow_login_requested:
        _flow_login_requested = False
        return {"task_id": None, "login_requested": True}
    now = time.time()
    for tid, task in list(_flow_tasks.items()):
        if task["status"] == "pending":
            if now - task["created_at"] > 900:  # 15遺?珥덇낵 ?먮룞 ??젣
                del _flow_tasks[tid]
                continue
            task["status"] = "sent"
            return task
    return {"task_id": None}

@app.post("/api/flow/task/{task_id}/complete")
async def flow_complete(task_id: str, req: Request):
    """濡쒖뺄 ?뚯빱媛 Flow ?묒뾽 ?꾨즺/?ㅽ뙣 蹂닿퀬"""
    body = await req.json()
    if task_id in _flow_tasks:
        _flow_tasks[task_id]["status"] = body.get("status", "done")
        _flow_tasks[task_id]["result_url"] = body.get("result_url", "")
        _flow_tasks[task_id]["error"] = body.get("error", "")
    return {"ok": True}

@app.get("/api/flow/task/{task_id}/status")
async def flow_task_status(task_id: str):
    """???꾨줎?몄뿏?쒓? ?묒뾽 ?곹깭 ?대쭅"""
    task = _flow_tasks.get(task_id)
    if not task:
        return {"status": "not_found"}
    return {
        "status":     task["status"],
        "result_url": task.get("result_url", ""),
        "error":      task.get("error", ""),
        "mode":       task.get("mode", ""),
    }

@app.get("/api/flow/tasks")
async def flow_list_tasks():
    """?꾩옱 ?먯뿉 ?덈뒗 紐⑤뱺 Flow ?묒뾽 紐⑸줉 (?붾쾭洹몄슜)"""
    return {"tasks": list(_flow_tasks.values())}

@app.post("/api/flow/upload")
async def flow_upload(file: UploadFile = File(...)):
    """濡쒖뺄 ?뚯빱媛 Flow 寃곌낵臾??대?吏/?곸긽)???쒕쾭???낅줈??""
    out_dir = "outputs"
    os.makedirs(out_dir, exist_ok=True)
    safe_name = f"flow_{int(time.time())}_{file.filename}"
    save_path = os.path.join(out_dir, safe_name)
    with open(save_path, "wb") as f:
        f.write(await file.read())
    url = f"{SERVER_URL}/download-audio/{safe_name}"
    return {"status": "ok", "url": url}

@app.post("/api/export-xml")
async def api_export_xml(req: RenderRequest):
    try:
        from renderer import export_premiere_xml
        result = export_premiere_xml(req)
        if result.get("status") != "success":
            return JSONResponse(status_code=500, content={"status": "error", "message": "XML ?앹꽦 ?ㅽ뙣"})

        import zipfile as _zf
        out_dir = "outputs"
        xml_path = os.path.join(out_dir, result["xml_file"])
        srt_path = os.path.join(out_dir, result["srt_file"])
        zip_name = f"premiere_export_{int(time.time())}.zip"
        zip_path = os.path.join(out_dir, zip_name)

        with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED) as zf:
            if os.path.exists(xml_path):
                zf.write(xml_path, result["xml_file"])
            if os.path.exists(srt_path):
                zf.write(srt_path, result["srt_file"])

        return {"status": "success", "zip_file": zip_name, "file_count": 2,
                "xml_file": result["xml_file"], "srt_file": result["srt_file"]}
    except Exception as e:
        logger.error(f"XML ?대낫?닿린 ?ㅽ뙣: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

_whisper_model = None

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        # PyInstaller 踰덈뱾: _MEIPASS瑜?PATH??異붽???whisper媛 ffmpeg 李얜룄濡?蹂댁옣
        _meipass = getattr(sys, '_MEIPASS', None)
        if _meipass and _meipass not in os.environ.get('PATH', ''):
            os.environ['PATH'] = _meipass + os.pathsep + os.environ.get('PATH', '')
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model_name = "small" if device == "cuda" else "base"
        _whisper_model = whisper.load_model(model_name, device=device)
    return _whisper_model

@app.post("/api/analyze-audio")
async def analyze_audio(file: UploadFile = File(...)):
    """Whisper濡??뚯꽦 ?뚯씪??遺꾩꽍?섏뿬 ?⑥뼱蹂???대컢 ?뺣낫 諛섑솚"""
    try:
        import whisper as _whisper
    except ImportError:
        return JSONResponse(status_code=503, content={"status": "error", "message": "Whisper 誘몄꽕移?(濡쒖뺄 ?쒕쾭 ?꾩슜 湲곕뒫)"})

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    try:
        logger.info(f"?뚯꽦 遺꾩꽍 ?쒖옉: {file.filename}")
        with os.fdopen(tmp_fd, 'wb') as f:
            f.write(await file.read())

        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = _whisper.load_model("base", device=device)
        result = model.transcribe(tmp_path, language="ko", verbose=False, word_timestamps=True)

        timings = []
        if "segments" in result:
            for segment in result["segments"]:
                if "words" in segment:
                    for word in segment["words"]:
                        timings.append({"word": word["word"], "start": word["start"], "end": word["end"]})

        full_text = result.get("text", "").strip()
        logger.info(f"?뚯꽦 遺꾩꽍 ?꾨즺: {len(timings)}媛??⑥뼱 媛먯?")
        return {"status": "success", "text": full_text, "timings": timings, "duration": result.get("duration", 0)}
    except Exception as e:
        logger.error(f"?뚯꽦 遺꾩꽍 ?ㅽ뙣: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

@app.post("/api/autosave")
async def post_autosave(request: Request):
    import json as _json
    data = await request.json()
    os.makedirs("outputs", exist_ok=True)
    with open("outputs/autosave.json", "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False)
    return {"status": "ok"}

@app.get("/api/autosave")
async def get_autosave():
    import json as _json
    path = "outputs/autosave.json"
    if not os.path.exists(path):
        return {"status": "not_found"}
    with open(path, "r", encoding="utf-8") as f:
        data = _json.load(f)
    return {"status": "ok", "data": data}

@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = os.path.join("outputs", filename)
    if not os.path.exists(file_path):
        return {"error": "File not found"}
    ext = filename.rsplit(".", 1)[-1].lower()
    media_map = {"mp4": "video/mp4", "zip": "application/zip", "mp3": "audio/mpeg", "wav": "audio/wav", "png": "image/png", "jpg": "image/jpeg"}
    media_type = media_map.get(ext, "application/octet-stream")
    return FileResponse(path=file_path, filename=filename, media_type=media_type)

@app.post("/api/save-video-upload")
async def save_video_upload(request: Request):
    """WebCodecs ?뚮뜑留?寃곌낵瑜??ㅽ듃由щ컢?쇰줈 諛쏆븘 outputs?????""
    try:
        os.makedirs("outputs", exist_ok=True)
        filename = f"video_{int(time.time())}.mp4"
        file_path = os.path.join("outputs", filename)
        body = await request.body()
        with open(file_path, "wb") as f:
            f.write(body)
        abs_path = os.path.abspath(file_path)
        return {"status": "ok", "filename": filename, "path": abs_path}
    except Exception as e:
        logger.error(f"save-video-upload ?ㅻ쪟: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/output-path/{filename}")
async def get_output_path(filename: str):
    file_path = os.path.join("outputs", filename)
    abs_path = os.path.abspath(file_path)
    if os.path.exists(abs_path):
        return {"status": "ok", "path": abs_path}
    return JSONResponse(status_code=404, content={"status": "error", "message": "?뚯씪 ?놁쓬"})

@app.post("/api/save-audio")
async def save_audio_named(request: Request):
    import base64 as _b64
    data = await request.json()
    b64 = data.get("data", "")
    filename = data.get("filename", f"audio_{int(time.time())}.wav")
    if not b64:
        return JSONResponse(status_code=400, content={"status": "error", "message": "?곗씠???놁쓬"})
    try:
        os.makedirs("outputs", exist_ok=True)
        file_path = os.path.join("outputs", filename)
        with open(file_path, "wb") as f:
            f.write(_b64.b64decode(b64))
        return {"status": "ok", "filename": filename, "url": f"/download-audio/{filename}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/download-audio/{filename}")
async def download_audio_file(filename: str, request: Request):
    file_path = os.path.join("outputs", filename)
    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    ext = filename.rsplit(".", 1)[-1].lower()
    media_type = {
        "wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
        "mp4": "video/mp4", "webm": "video/webm", "gif": "image/gif",
        "zip": "application/zip",
    }.get(ext, "application/octet-stream")
    # filename ?뚮씪誘명꽣 ?쒓굅 ??Content-Disposition: inline (釉뚮씪?곗? ?몃씪???ъ깮)
    # FileResponse??Starlette?먯꽌 Range ?붿껌 ?먮룞 吏??
    return FileResponse(path=file_path, media_type=media_type, headers={"Accept-Ranges": "bytes"})

@app.post("/api/upload-audio")
async def upload_audio(file: UploadFile = File(...)):
    try:
        os.makedirs("outputs", exist_ok=True)
        filename = f"audio_{int(time.time())}_{file.filename}"
        file_path = os.path.join("outputs", filename)
        body = await file.read()
        with open(file_path, "wb") as f:
            f.write(body)
        abs_path = os.path.abspath(file_path)
        return {"status": "success", "filename": filename, "path": abs_path}
    except Exception as e:
        logger.error(f"upload-audio ?ㅻ쪟: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/save-image")
async def save_image(request: Request):
    """base64 ?대?吏瑜?outputs ?대뜑????????ㅼ슫濡쒕뱶 URL 諛섑솚"""
    data = await request.json()
    b64 = data.get("data", "") or data.get("image", "")
    # data URL ?뺤떇(data:image/png;base64,xxx)?대㈃ base64 遺遺꾨쭔 異붿텧
    if b64 and "," in b64:
        b64 = b64.split(",", 1)[1]
    ext = data.get("ext", "png")
    if not ext or ext == "png":
        raw = data.get("image", data.get("data", ""))
        if "jpeg" in raw or "jpg" in raw:
            ext = "jpg"
    filename = data.get("filename", f"image_{int(time.time())}.{ext}")
    if not b64:
        return JSONResponse(status_code=400, content={"status": "error", "message": "?곗씠???놁쓬"})
    try:
        import base64 as _b64
        os.makedirs("outputs", exist_ok=True)
        file_path = os.path.join("outputs", filename)
        with open(file_path, "wb") as f:
            f.write(_b64.b64decode(b64))
        return {"status": "ok", "filename": filename, "url": f"/download-image/{filename}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/download-image/{filename}")
async def download_image_file(filename: str):
    file_path = os.path.join("outputs", filename)
    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    ext = filename.rsplit(".", 1)[-1].lower()
    media_type = "image/png" if ext == "png" else "image/jpeg" if ext in ("jpg","jpeg") else "application/octet-stream"
    return FileResponse(path=file_path, filename=filename, media_type=media_type)

@app.post("/api/save-all-images")
async def save_all_images(request: Request):
    """?щ윭 ?대?吏瑜?outputs ?대뜑????ν븯怨?ZIP?쇰줈 臾띠뼱??寃쎈줈 諛섑솚"""
    import base64 as _b64
    import zipfile
    data = await request.json()
    images = data.get("images", [])        # [{filename, data, ext}, ...] ??base64
    server_files = data.get("server_files", [])  # [{zipName, filename}, ...] ???쒕쾭 ????뚯씪
    if not images and not server_files:
        return JSONResponse(status_code=400, content={"status": "error", "message": "?대?吏 ?놁쓬"})
    try:
        os.makedirs("outputs", exist_ok=True)
        zip_name = f"images_{int(time.time())}.zip"
        zip_path = os.path.join("outputs", zip_name)
        saved = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            # base64 ?대?吏
            for img in images:
                fn = img.get("filename", f"scene_{int(time.time())}.png")
                b64 = img.get("data", "")
                if not b64:
                    continue
                zf.writestr(fn, _b64.b64decode(b64))
                saved.append(fn)
            # ?쒕쾭 ????뚯씪
            for sf in server_files:
                src = os.path.join("outputs", sf.get("filename", ""))
                zip_entry = sf.get("zipName", sf.get("filename", ""))
                if src and os.path.exists(src):
                    zf.write(src, zip_entry)
                    saved.append(zip_entry)
        if not saved:
            return JSONResponse(status_code=400, content={"status": "error", "message": "?대?吏 ?놁쓬"})
        return {"status": "ok", "zip": zip_name, "count": len(saved), "path": f"outputs/{zip_name}"}
    except Exception as e:
        logger.error(f"save-all-images ?ㅻ쪟: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/youtube-transcripts")
async def youtube_transcripts(request: Request):
    """?좏뒠釉?梨꾨꼸 URL ??理쒓렐 ?곸긽 2~3媛??먮쭑 ?먮룞 ?섏쭛"""
    import re
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
    except ImportError:
        return JSONResponse(status_code=500, content={"status": "error", "message": "youtube-transcript-api 誘몄꽕移?})

    data = await request.json()
    channel_url = data.get("channel_url", "").strip()
    video_urls  = data.get("video_urls", [])   # 媛쒕퀎 ?곸긽 URL 紐⑸줉 (梨꾨꼸 ???吏곸젒 吏??媛??

    def extract_video_id(url: str):
        m = re.search(r'(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})', url)
        return m.group(1) if m else None

    def fetch_transcript(video_id: str):
        try:
            api = YouTubeTranscriptApi()
            try:
                transcript = api.fetch(video_id, languages=['ko'])
            except Exception:
                try:
                    transcript = api.fetch(video_id, languages=['en'])
                except Exception:
                    transcript = api.fetch(video_id)
            return " ".join(s.text.replace("\n", " ") for s in transcript)
        except (NoTranscriptFound, TranscriptsDisabled):
            return None
        except Exception:
            return None

    # ?곸긽 URL 紐⑸줉??吏곸젒 二쇱뼱吏?寃쎌슦
    if video_urls:
        results = []
        for url in video_urls[:5]:
            vid = extract_video_id(url)
            if not vid:
                continue
            txt = fetch_transcript(vid)
            if txt:
                results.append({"video_id": vid, "url": url, "transcript": txt})
        if not results:
            return JSONResponse(status_code=404, content={"status": "error", "message": "?먮쭑??媛?몄삱 ???놁뒿?덈떎. ?먮쭑??鍮꾪솢?깊솕???곸긽?닿굅??吏?먰븯吏 ?딅뒗 ?뺤떇?낅땲??"})
        return {"status": "ok", "transcripts": results}

    # 梨꾨꼸 URL?먯꽌 理쒓렐 ?곸긽 ID 異붿텧 (yt-dlp ?놁씠 媛꾨떒 ?ㅽ겕?섑븨)
    if not channel_url:
        return JSONResponse(status_code=400, content={"status": "error", "message": "channel_url ?먮뒗 video_urls ?꾩슂"})

    try:
        import urllib.request
        import html
        # 梨꾨꼸 ?섏씠吏 HTML?먯꽌 ?곸긽 ID 異붿텧
        req = urllib.request.Request(channel_url + "/videos", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="ignore")
        vids = list(dict.fromkeys(re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', page)))[:5]
        if not vids:
            return JSONResponse(status_code=404, content={"status": "error", "message": "梨꾨꼸?먯꽌 ?곸긽??李얠쓣 ???놁뒿?덈떎. ?곸긽 URL??吏곸젒 ?낅젰?댁＜?몄슂."})
        results = []
        for vid in vids:
            txt = fetch_transcript(vid)
            if txt:
                results.append({"video_id": vid, "url": f"https://youtu.be/{vid}", "transcript": txt})
            if len(results) >= 3:
                break
        if not results:
            return JSONResponse(status_code=404, content={"status": "error", "message": "?먮쭑???덈뒗 ?곸긽??李얠? 紐삵뻽?듬땲?? ?곸긽 URL??吏곸젒 ?낅젰?댁＜?몄슂."})
        return {"status": "ok", "transcripts": results}
    except Exception as e:
        logger.error(f"youtube-transcripts ?ㅻ쪟: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


# ?? GrokBridge API ??

@app.get("/api/grok/status")
async def grok_status():
    """GrokBridge ?곌껐 ?곹깭 諛섑솚"""
    if not _grok_available or not _grok_bridge:
        return {"status": "ok", "available": False, "running": False, "connected": False, "error": _grok_load_error[:300] if _grok_load_error else ""}
    return {
        "status": "ok",
        "available": True,
        "running": _grok_bridge.is_running,
        "connected": _grok_bridge.is_connected,
        "download_folder": _grok_bridge.download_folder or "",
        "queue_paused": _grok_bridge.queue_paused,
        "queueCount": getattr(_grok_bridge, 'queue_count', 0),
        "isProcessing": getattr(_grok_bridge, 'is_processing', False),
        "currentSceneNo": getattr(_grok_bridge, 'current_scene_no', None),
        "completedCount": getattr(_grok_bridge, 'completed_count', 0)
    }

@app.post("/api/grok/start")
async def grok_start():
    """GrokBridge WebSocket ?쒕쾭 ?쒖옉 (?ы듃 9876)"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    try:
        if not _grok_bridge.is_running:
            await _grok_bridge.start()
        return {"status": "ok", "running": _grok_bridge.is_running}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/grok/open-chrome")
async def grok_open_chrome():
    """Chrome???닿퀬 grok.com?쇰줈 ?대룞"""
    import subprocess as _sp
    url = "https://grok.com"
    if sys.platform == "win32":
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        chrome_exe = next((p for p in chrome_paths if os.path.exists(p)), None)
        try:
            if chrome_exe:
                _sp.Popen([chrome_exe, url])
            else:
                import webbrowser; webbrowser.open(url)
            return {"status": "ok"}
        except Exception as e:
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    else:
        return JSONResponse(status_code=503, content={"status": "error", "message": "Chrome ?먮룞 ?ㅽ뻾? Windows ?꾩슜 湲곕뒫?낅땲??"})

@app.post("/api/grok/stop")
async def grok_stop():
    """GrokBridge WebSocket ?쒕쾭 以묒?"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    try:
        await _grok_bridge.stop()
        return {"status": "ok", "running": _grok_bridge.is_running}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

class GrokSetFolderReq(BaseModel):
    download_folder: str

@app.post("/api/grok/set-folder")
async def grok_set_folder(req: GrokSetFolderReq):
    """?ㅼ슫濡쒕뱶 ?대뜑 ?ㅼ젙"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    try:
        folder = req.download_folder.strip()
        if not folder:
            return JSONResponse(status_code=400, content={"status": "error", "message": "?대뜑 寃쎈줈 ?꾩슂"})
        os.makedirs(folder, exist_ok=True)
        _grok_bridge.download_folder = folder
        if _grok_bridge.is_connected:
            await _grok_bridge.set_project_path(folder, folder)
        return {"status": "ok", "download_folder": folder}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

class GrokTask(BaseModel):
    sceneNo: int
    prompt: str
    imageBase64: Optional[str] = None
    koreanText: Optional[str] = None

class GrokSendTasksReq(BaseModel):
    tasks: List[GrokTask]
    download_folder: Optional[str] = None
    useCrop169: Optional[bool] = False
    useKoreanText: Optional[bool] = False

def _process_grok_image_and_prompt(t: GrokTask, use_crop169: bool, use_korean_text: bool):
    img_b64 = t.imageBase64 or ""
    prompt = t.prompt
    
    if use_crop169 and img_b64:
        try:
            import base64
            from io import BytesIO
            from PIL import Image, ImageChops
            
            header = ""
            encoded = img_b64
            if "," in img_b64:
                header, encoded = img_b64.split(",", 1)
            
            img_data = base64.b64decode(encoded)
            img = Image.open(BytesIO(img_data)).convert("RGB")
            
            # Remove white letterboxing
            bg = Image.new(img.mode, img.size, (255, 255, 255))
            diff = ImageChops.difference(img, bg)
            diff = ImageChops.add(diff, diff, 2.0, -100)
            bbox = diff.getbbox()
            if bbox:
                img = img.crop(bbox)
            
            # Crop to 16:9
            target_ratio = 16.0 / 9.0
            w, h = img.size
            current_ratio = w / h
            
            if abs(current_ratio - target_ratio) > 0.01:
                if current_ratio > target_ratio:
                    new_w = int(h * target_ratio)
                    left = (w - new_w) // 2
                    img = img.crop((left, 0, left + new_w, h))
                else:
                    new_h = int(w / target_ratio)
                    top = (h - new_h) // 2
                    img = img.crop((0, top, w, top + new_h))
                    
            buffered = BytesIO()
            img.save(buffered, format="JPEG", quality=95)
            new_encoded = base64.b64encode(buffered.getvalue()).decode("utf-8")
            
            if header:
                img_b64 = f"{header},{new_encoded}"
            else:
                img_b64 = f"data:image/jpeg;base64,{new_encoded}"
        except Exception as e:
            logger.error(f"Image crop failed: {e}")
            pass

    if use_korean_text and t.koreanText:
        prompt = f"{prompt}\n\n[Korean Context for exact rendering if needed: {t.koreanText}]"
        
    return img_b64, prompt

@app.post("/api/grok/send-tasks")
async def grok_send_tasks(req: GrokSendTasksReq):
    """???묒뾽 紐⑸줉??MakeLensAuto ?뺤옣?꾨줈洹몃옩?쇰줈 ?꾩넚"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    if not _grok_bridge.is_running:
        return JSONResponse(status_code=400, content={"status": "error", "message": "GrokBridge ?쒕쾭媛 ?ㅽ뻾 以묒씠 ?꾨떃?덈떎. 癒쇱? ?쒕쾭瑜??쒖옉?댁＜?몄슂."})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "MakeLensAuto ?뺤옣?꾨줈洹몃옩???곌껐?섏? ?딆븯?듬땲??"})
    try:
        global _grok_session_start_time, _grok_completed_videos, _grok_task_cache
        # ???몄뀡 ?쒖옉 ???댁쟾 ?묒뾽 痍⑥냼 (?뺤옣???湲곗뿴 珥덇린??
        await _grok_bridge.cancel_all_tasks()
        
        import asyncio
        await asyncio.sleep(0.5)  # ?뺤옣?깆씠 CANCEL_ALL??泥섎━?섍퀬 ?먮? 鍮꾩슱 ?쒓컙???쎄컙 以?

        # ?댁쟾 ?꾨즺 紐⑸줉 珥덇린??+ ?꾩옱 ?쒓컖 湲곕줉
        _grok_completed_videos = {}
        _grok_session_start_time = time.time()

        # ?ㅼ슫濡쒕뱶 ?대뜑 ?ㅼ젙
        if req.download_folder:
            folder = req.download_folder.strip()
            os.makedirs(folder, exist_ok=True)
            _grok_bridge.download_folder = folder
            await _grok_bridge.set_project_path(folder, folder)

        # 諛곗튂 紐⑤뱶 ?ㅼ젙: ?ㅽ뙣?대룄 ?ㅼ쓬 ?묒뾽 怨꾩냽 吏꾪뻾
        await _grok_bridge.update_settings({
            "continueOnError": True,
            "maxRetryCount": 3
        })

        tasks_info = []
        for t in req.tasks:
            proc_img, proc_prompt = _process_grok_image_and_prompt(t, req.useCrop169, req.useKoreanText)
            tasks_info.append({
                "sceneNo": t.sceneNo,
                "prompt": proc_prompt,
                "imageBase64": proc_img,
                "folderName": ""
            })

        # 罹먯떆????ν븯???ъ떎???붿껌 ???ъ슜
        for t in tasks_info:
            _grok_task_cache[t["sceneNo"]] = t

        # ?ъ씠?쒗뙣?먯뿉 ?꾩껜 紐⑸줉 誘몃━蹂닿린 癒쇱? ?꾩넚
        preview_info = [{"sceneNo": t["sceneNo"], "prompt": t["prompt"], "folderName": ""} for t in tasks_info]
        await _grok_bridge.send_queue_preview(preview_info, is_new_run=True)

        await _grok_bridge.add_all_tasks(tasks_info)
        return {"status": "ok", "sent": len(tasks_info)}
    except Exception as e:
        logger.error(f"grok/send-tasks ?ㅻ쪟: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/grok/completed")
async def grok_completed():
    """?꾨즺??Grok ?곸긽 紐⑸줉 諛섑솚 (?꾨줎?몄뿏?쒓? ?대쭅?섏뿬 ?ъ뿉 ?곸슜)"""
    import re

    # ?몄뀡 ?쒖옉 ??吏???대뜑???앹꽦??scene_XX_grok.mp4 ?뚯씪留??ㅼ틪
    if _grok_session_start_time > 0 and _grok_bridge and _grok_bridge.download_folder and os.path.isdir(_grok_bridge.download_folder):
        folder = _grok_bridge.download_folder
        for fname in os.listdir(folder):
            m = re.match(r'^scene_(\d+)_grok\.mp4$', fname)
            if m:
                scene_no = int(m.group(1))
                fpath = os.path.join(folder, fname)
                if scene_no not in _grok_completed_videos:
                    if os.path.getmtime(fpath) >= _grok_session_start_time:
                        _grok_completed_videos[scene_no] = fpath

    result = []
    for scene_no, video_path in list(_grok_completed_videos.items()):
        result.append({
            "sceneNo": scene_no,
            "videoUrl": f"/api/grok/video/{scene_no}",
            "localFilename": f"scene_{scene_no:02d}_grok.mp4",
        })
    failed = [{"sceneNo": sn, "error": msg} for sn, msg in list(_grok_failed_scenes.items())]
    return {"completed": result, "failed": failed}

@app.get("/api/grok/video/{scene_no}")
async def grok_video_file(scene_no: int):
    """??踰덊샇濡?Grok ?앹꽦 ?곸긽 ?뚯씪 ?쒓났"""
    video_path = _grok_completed_videos.get(scene_no)
    if not video_path or not os.path.exists(video_path):
        return JSONResponse(status_code=404, content={"error": "?곸긽 ?놁쓬"})
    return FileResponse(path=video_path, media_type="video/mp4",
                        filename=os.path.basename(video_path))

@app.post("/api/grok/cancel")
async def grok_cancel():
    """?꾩껜 ?묒뾽 痍⑥냼"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    try:
        await _grok_bridge.cancel_all_tasks()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/api/grok/resume")
async def grok_resume():
    """?ㅽ뙣濡?硫덉텣 ?湲곗뿴 ?댁뼱???ㅽ뻾"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "?뺤옣?꾨줈洹몃옩???곌껐?섏? ?딆븯?듬땲??"})
    try:
        await _grok_bridge.resume_queue()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


class GrokUpdateSettingsReq(BaseModel):
    continueOnError: Optional[bool] = None
    maxRetryCount: Optional[int] = None
    autoDownload: Optional[bool] = None
    upscaleBeforeDownload: Optional[bool] = None

@app.post("/api/grok/update-settings")
async def grok_update_settings(req: GrokUpdateSettingsReq):
    """?뺤옣???고????ㅼ젙 蹂寃?""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "?뺤옣?꾨줈洹몃옩???곌껐?섏? ?딆븯?듬땲??"})
    try:
        settings = {k: v for k, v in req.dict().items() if v is not None}
        await _grok_bridge.update_settings(settings)
        return {"status": "ok", "applied": settings}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


class GrokRetrySceneReq(BaseModel):
    sceneNo: int
    prompt: str
    imageBase64: Optional[str] = None

@app.post("/api/grok/retry-scene")
async def grok_retry_scene(req: GrokRetrySceneReq):
    """?뱀젙 ???ъ떎??""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge ?놁쓬"})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "?뺤옣?꾨줈洹몃옩???곌껐?섏? ?딆븯?듬땲??"})
    try:
        await _grok_bridge.send_retry_scene_data(
            scene_no=req.sceneNo,
            prompt=req.prompt,
            image_base64=req.imageBase64 or "",
            folder_name=""
        )
        return {"status": "ok", "sceneNo": req.sceneNo}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


import subprocess

def _qwen_bat_path() -> str:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qwen_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            bat = cfg.get("qwen_bat_path", "")
            if bat and os.path.exists(bat):
                return bat
        except Exception:
            pass
    for c in [r"C:\Qwen3-TTS\Run Qwen3 TTS.bat", r"D:\Qwen3-TTS\Run Qwen3 TTS.bat",
              r"D:\Qwen3-TTS-API\start.bat", r"C:\Qwen3-TTS-API\start.bat"]:
        if os.path.exists(c):
            return c
    return ""

@app.post("/api/start-qwen-server")
async def start_qwen_server():
    if sys.platform != "win32":
        return JSONResponse({"ok": False, "error": "Qwen3-TTS??濡쒖뺄 Windows ?꾩슜 湲곕뒫?낅땲??"}, status_code=503)
    bat_path = _qwen_bat_path()
    if not bat_path:
        return JSONResponse({"ok": False, "error": "Qwen3-TTS ?곗쿂瑜?李얠쓣 ???놁뒿?덈떎."}, status_code=404)
    try:
        subprocess.Popen(bat_path, creationflags=subprocess.CREATE_NEW_CONSOLE, shell=True)
        return JSONResponse({"ok": True, "bat": bat_path})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


_whisper_model = None

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        try:
            _whisper_model = WhisperModel("large-v3", device="cuda", compute_type="float16")
            logger.info("[Whisper] large-v3 CUDA 濡쒕뱶 ?꾨즺")
        except Exception:
            _whisper_model = WhisperModel("large-v3", device="cpu", compute_type="int8")
            logger.info("[Whisper] large-v3 CPU 濡쒕뱶 ?꾨즺")
    return _whisper_model

@app.post("/api/whisper-align")
async def whisper_align(request: Request):
    try:
        from faster_whisper import WhisperModel as _WM
    except ImportError:
        return JSONResponse(status_code=503, content={"status": "error", "message": "faster-whisper 誘몄꽕移?(濡쒖뺄 ?쒕쾭 ?꾩슜 湲곕뒫)"})
    data = await request.json()
    audio_url = data.get("audio_url", "")
    if not audio_url:
        return JSONResponse(status_code=400, content={"status": "error", "message": "audio_url ?놁쓬"})
    # URL ???뚯씪 寃쎈줈 蹂??
    if audio_url.startswith("/download-audio/"):
        filename = audio_url.replace("/download-audio/", "")
        audio_path = os.path.join("outputs", filename)
    else:
        return JSONResponse(status_code=400, content={"status": "error", "message": "吏?먰븯吏 ?딅뒗 URL ?뺤떇"})
    if not os.path.exists(audio_path):
        return JSONResponse(status_code=404, content={"status": "error", "message": f"?뚯씪 ?놁쓬: {audio_path}"})
    try:
        model = await asyncio.to_thread(_get_whisper_model)
        def _transcribe():
            segs, _ = model.transcribe(audio_path, word_timestamps=True, language="ko")
            words = []
            for seg in segs:
                if seg.words:
                    for w in seg.words:
                        t = (w.word or "").strip()
                        if t:
                            words.append({"text": t, "start": round(w.start, 3), "end": round(w.end, 3)})
            return words
        timings = await asyncio.to_thread(_transcribe)
        return {"status": "ok", "timings": timings}
    except Exception as e:
        logger.error(f"[Whisper] ?ㅻ쪟: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


if __name__ == "__main__":
    import multiprocessing
    import threading
    import time
    multiprocessing.freeze_support()
    os.makedirs("outputs", exist_ok=True)
    os.makedirs("outputs/temp", exist_ok=True)

    try:
        import webview

        def _run_server():
            async def _start_with_bridge():
                if _grok_available and _grok_bridge:
                    try:
                        await _grok_bridge.start()
                        print("[ScriptStudio] GrokBridge WebSocket ?쒕쾭 ?쒖옉: ws://localhost:9876")
                    except Exception as _be:
                        print(f"[ScriptStudio] GrokBridge ?쒖옉 ?ㅽ뙣 (臾댁떆): {_be}")
                config = uvicorn.Config(app, host="127.0.0.1", port=8000, reload=False)
                server = uvicorn.Server(config)
                await server.serve()
            asyncio.run(_start_with_bridge())

        t = threading.Thread(target=_run_server, daemon=True)
        t.start()
        time.sleep(1.5)  # ?쒕쾭 湲곕룞 ?湲?

        webview.create_window(
            "ScriptStudio",
            "http://127.0.0.1:8000",
            width=1440, height=900,
            resizable=True,
            min_size=(900, 600),
        )
        webview.start()

    except ImportError:
        # pywebview 誘몄꽕移???釉뚮씪?곗?濡??대갚
        import webbrowser
        def _open_browser():
            time.sleep(1.2)
            webbrowser.open("http://localhost:8000")
        threading.Thread(target=_open_browser, daemon=True).start()
        uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)

