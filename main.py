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
from renderer import render_video
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# GrokBridge 연동 (MakeLensAuto 없이 로컬 복사본 사용)
_grok_bridge = None
_grok_available = False
_grok_load_error = ""
# 완료된 Grok 영상 목록: {sceneNo: videoPath} — 프론트엔드가 폴링해서 씬에 적용
_grok_completed_videos: dict = {}
# 실패한 Grok 씬 목록: {sceneNo: errorMsg}
_grok_failed_scenes: dict = {}
# 작업 전송 시각 (이 시각 이후 생성된 파일만 감지)
_grok_session_start_time: float = 0.0
# 씬 번호별 원본 작업 데이터 캐시 (재실행 요청 시 제공용)
_grok_task_cache: dict = {}

def _try_load_grok_bridge():
    global _grok_bridge, _grok_available, _grok_load_error
    import traceback as _tb
    # PyInstaller 번들 경로를 sys.path에 추가
    _meipass = getattr(sys, '_MEIPASS', None)
    if _meipass and _meipass not in sys.path:
        sys.path.insert(0, _meipass)
    # 현재 파일 디렉토리도 추가
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
                print(f"[ScriptStudio] 씬 {scene_no} 영상 완료: {target_path}")

        _grok_bridge.on_video_moved = _on_video_moved

        def _on_task_failed(data):
            scene_no = data.get('sceneNo')
            error_msg = data.get('error', '생성 실패')
            if scene_no:
                _grok_failed_scenes[int(scene_no)] = error_msg
                print(f"[ScriptStudio] 씬 {scene_no} 생성 실패: {error_msg}")

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
                    print(f"재실행 요청 처리 실패: {e}")
            else:
                try:
                    import asyncio
                    loop = asyncio.get_running_loop()
                    loop.create_task(_grok_bridge.send_retry_scene_error(
                        scene_no, "원본 작업 데이터를 찾을 수 없습니다. 본 프로그램에서 다시 전송해주세요."
                    ))
                except Exception:
                    pass

        _grok_bridge.on_retry_scene_request = _on_retry_scene_request

    except Exception as _e:
        _grok_bridge = None
        _grok_available = False
        _grok_load_error = _tb.format_exc()
        # 에러를 파일에 기록 (frozen exe에서 console=False이므로)
        try:
            _log_path = os.path.join(os.path.expanduser("~"), "grok_bridge_error.txt")
            with open(_log_path, "w", encoding="utf-8") as _lf:
                _lf.write(_grok_load_error)
        except Exception:
            pass

_try_load_grok_bridge()

# Vertex AI 서비스 계정 키 경로 (없으면 Vertex AI 비활성화)
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

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# ── Kaggle 자동 스케줄러 ──
_KST = pytz.timezone("Asia/Seoul")
_KAGGLE_KERNEL_SLUG_A = os.environ.get("KAGGLE_KERNEL_SLUG_A", "notebookc3d9872480")   # T4 커널 (주)
_KAGGLE_KERNEL_SLUG_B = os.environ.get("KAGGLE_KERNEL_SLUG_B", "notebookc3d9872480")   # T4 커널 (동일)

async def _startup_kaggle_if_offline():
    """Railway 재시작 후 URL이 없으면 60초 대기 후 Kaggle 자동 재시작"""
    await asyncio.sleep(60)
    if not _kaggle_tts_url:
        logger.info("[재시작 감지] Kaggle URL 없음 → 자동 재시작 시도")
        await _auto_start_kaggle(_KAGGLE_KERNEL_SLUG_B)

async def _auto_start_kaggle(slug: str):
    logger.info(f"[스케줄러] Kaggle 자동 시작: {slug}")
    try:
        loop = asyncio.get_event_loop()
        rc, out = await loop.run_in_executor(None, lambda: _kaggle_push_sync_slug(slug))
        logger.info(f"[스케줄러] push rc={rc} out={out[:200]}")
    except Exception as e:
        logger.error(f"[스케줄러] 오류: {e}")

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
    logger.info("[스케줄러] Kaggle 자동 시작 등록: 노트북A=자정, 노트북B=정오 (KST)")
    # Railway 재시작 시 URL이 비어있으면 자동으로 Kaggle 커널 재시작
    asyncio.create_task(_startup_kaggle_if_offline())

@app.on_event("shutdown")
async def shutdown_scheduler():
    _scheduler.shutdown()

# Qwen3-TTS 로컬 (Gradio) 라우트 등록
try:
    from fresh_qwen_local import install_fresh_qwen_local_routes
    install_fresh_qwen_local_routes(app)
except Exception as _fql_err:
    logger.warning(f"[fresh_qwen_local] 라우트 등록 실패 (무시): {_fql_err}")

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 422 오류 상세 로깅 핸들러
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    safe_errors = []
    for e in exc.errors():
        safe_errors.append({
            "loc": [str(x) for x in e.get("loc", [])],
            "msg": str(e.get("msg", "")),
            "type": str(e.get("type", "")),
        })
        logger.error(f"  [422] 필드: {e.get('loc')} | 오류: {e.get('msg')} | 타입: {e.get('type')}")
    return JSONResponse(
        status_code=422,
        content={"status": "error", "message": "요청 데이터 형식 오류", "detail": safe_errors}
    )

# 전역 예외 핸들러 — 어떤 예외든 JSON으로 반환
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    logger.error(f"[전역오류] {exc}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": str(exc), "detail": tb[-1000:]}
    )

# 데이터 스키마
class SceneData(BaseModel):
    id: int
    script: str
    imgDataUrl: Optional[str] = None
    audioDataUrl: Optional[str] = None  # base64 mp3/wav
    audioPath: Optional[str] = None
    dur: float
    gapSec: float
    subtitleTimings: Optional[list] = None  # [{text, start, end}, ...]
    grokVideo: bool = False
    grokVideoPath: Optional[str] = None  # 실제 파일 경로 (서버에서 채움)
    useFade: bool = False
    zoom_type: Optional[str] = None  # zoom_in, zoom_out, pan_left, pan_right, pan_up, pan_down, none (None=전역설정 따름)

class RenderRequest(BaseModel):
    grok_download_folder: Optional[str] = None
    scenes: List[SceneData]
    w: int
    h: int
    fps: int
    subtitle_font: str = "맑은 고딕"
    subtitle_color: str = "&H00FFFFFF"
    subtitle_bg: str = "box"
    subtitle_size: int = 45
    transition_type: str = "hard"
    zoom_speed: float = 1.08
    use_zoompan: bool = True
    global_zoom_type: str = "zoom_in"  # zoom_in, zoom_out, pan_left, pan_right, pan_up, pan_down, none
    show_subtitle: bool = True

# 정적 파일 서빙을 위한 경로 설정
@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_path = resource_path("최종본.html")
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

# OAuth2 로그인 상태
_oauth_state: dict = {}  # flow, credentials, project_id

@app.get("/api/vertex-token")
async def get_vertex_token():
    """Vertex AI 토큰 반환: OAuth2 로그인 우선, 없으면 서비스 계정 키"""
    now = time.time()

    # ① OAuth2 로그인된 경우
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
            logger.error(f"OAuth 토큰 갱신 실패: {e}")
            return JSONResponse(status_code=401, content={"status": "error", "message": f"OAuth 토큰 갱신 실패: {e}"})

    # ② 서비스 계정 키 파일
    if not _vertex_key_path:
        return JSONResponse(status_code=404, content={"status": "error", "message": "로그인이 필요합니다 (Google 로그인 버튼 클릭)"})

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
        logger.error(f"Vertex AI 토큰 발급 실패: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# project_id 캐시 (서비스 계정용)
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
            return JSONResponse(status_code=400, content={"status": "error", "message": "서비스 계정 키 파일이 아닙니다"})
        project_id = key_data.get("project_id", "")
        save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vertex_key.json")
        with open(save_path, "wb") as f:
            f.write(contents)
        _vertex_key_path = save_path
        _vertex_project_id = project_id
        _vertex_token_cache = {"token": None, "expires_at": 0}
        return {"status": "ok", "project_id": project_id}
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "유효한 JSON 파일이 아닙니다"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ── Google OAuth2 로그인 엔드포인트 ──

class GoogleAuthStartReq(BaseModel):
    client_id: str
    client_secret: str
    project_id: str

@app.post("/api/auth/google/start")
async def google_auth_start(req: GoogleAuthStartReq):
    """OAuth2 인증 URL 생성 및 브라우저 오픈"""
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
        logger.error(f"OAuth 시작 오류: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/auth/google/callback")
async def google_auth_callback(code: str = "", error: str = "", state: str = ""):
    """OAuth2 콜백 — 인증 코드 교환"""
    if error:
        return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:40px'><h2>❌ 인증 취소됨</h2><p>{error}</p><p>이 창을 닫으세요.</p></body></html>")
    if not code:
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'><h2>❌ 코드 없음</h2><p>이 창을 닫으세요.</p></body></html>")

    flow = _oauth_state.get("flow")
    if not flow:
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'><h2>❌ 세션 만료</h2><p>다시 로그인해주세요.</p></body></html>")

    try:
        import os as _os
        _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        flow.fetch_token(code=code)
        creds = flow.credentials

        _oauth_state["credentials"] = creds
        _vertex_token_cache["token"] = creds.token
        _vertex_token_cache["expires_at"] = time.time() + 3600

        logger.info("Google OAuth2 로그인 성공")
        return HTMLResponse("""
<html><head><meta charset='utf-8'></head>
<body style='font-family:sans-serif;padding:40px;text-align:center;background:#0d1117;color:#e6edf3'>
  <h2 style='color:#36d68a'>✅ Google 로그인 성공!</h2>
  <p>이 창을 닫고 ScriptStudio로 돌아가세요.</p>
  <script>setTimeout(()=>window.close(),2000);</script>
</body></html>""")
    except Exception as e:
        logger.error(f"OAuth 콜백 오류: {e}")
        return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:40px'><h2>❌ 인증 오류</h2><p>{e}</p></body></html>")

@app.get("/api/auth/google/status")
async def google_auth_status():
    """로그인 상태 조회"""
    creds = _oauth_state.get("credentials")
    if creds and _vertex_token_cache.get("token"):
        return {"status": "ok", "logged_in": True, "project_id": _oauth_state.get("project_id", "")}
    # 서비스 계정 키가 있으면 그것도 '연결됨'으로 간주
    if _vertex_key_path:
        return {"status": "ok", "logged_in": True, "project_id": _vertex_project_id, "via": "service_account"}
    return {"status": "ok", "logged_in": False}

@app.post("/api/auth/google/logout")
async def google_auth_logout():
    """로그아웃"""
    _oauth_state.clear()
    _vertex_token_cache["token"] = None
    _vertex_token_cache["expires_at"] = 0
    return {"status": "ok"}

@app.get("/api/fonts")
async def api_fonts():
    fmap = load_font_map()
    fonts_list = sorted(list(fmap.keys()))
    if not fonts_list:
        fonts_list = ["맑은 고딕", "굴림", "돋움", "궁서", "Arial"]
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
    return {"status": "ok", "platform": sys.platform}

# Kaggle TTS 서버 URL 브로커
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

@app.post("/api/set-qwen-url")
async def set_qwen_url(req: Request):
    data = await req.json()
    secret = data.get("secret", "")
    if _KAGGLE_SECRET and secret != _KAGGLE_SECRET:
        return JSONResponse(status_code=401, content={"status": "error"})
    global _kaggle_tts_url, _kaggle_url_registered_at
    _kaggle_tts_url = data.get("url", "").rstrip("/")
    _kaggle_url_registered_at = time.time()
    _save_cached_url(_kaggle_tts_url)
    logger.info(f"Kaggle TTS URL 등록: {_kaggle_tts_url}")
    return {"status": "ok", "url": _kaggle_tts_url}

@app.get("/api/qwen-url")
async def get_qwen_url():
    if not _kaggle_tts_url:
        return JSONResponse(status_code=503, content={"status": "offline", "message": "TTS 서버가 오프라인입니다. Kaggle 노트북을 실행하세요."})
    return {"status": "ok", "url": _kaggle_tts_url}

_KAGGLE_USERNAME = os.environ.get("KAGGLE_USERNAME", "junryong")
_KAGGLE_KEY = os.environ.get("KAGGLE_KEY", "7a6425f7c4e48f1dc705fb462b94dc20")
_KAGGLE_KERNEL_SLUG = os.environ.get("KAGGLE_KERNEL_SLUG", "notebookc3d9872480")
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

# 2. transformers 버전 확인 후 필요할 때만 재설치
try:
    import transformers as _tr
    assert _tr.__version__ == "4.57.3"
    print(f"transformers {_tr.__version__} 이미 설치됨 — skip")
except (ImportError, AssertionError):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "transformers==4.57.3"])

# 3. 없는 패키지만 설치
_need = {p: n for p, n in {
    "fastapi": "fastapi", "uvicorn": "uvicorn", "python-multipart": "multipart",
    "soundfile": "soundfile", "librosa": "librosa", "einops": "einops",
    "pydub": "pydub", "scipy": "scipy", "sox": "sox", "onnxruntime": "onnxruntime",
    "pyyaml": "yaml", "accelerate": "accelerate"
}.items() if importlib.util.find_spec(n) is None}
if _need:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + list(_need.keys()))

# 4. qwen-tts 항상 재설치
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "-e", "/kaggle/working/tts"])

# auto_docstring patch
noop = ("from transformers.utils import ModelOutput, logging\\n"
        "def auto_docstring(*a, **kw):\\n"
        "    if len(a) == 1 and callable(a[0]): return a[0]\\n"
        "    return lambda fn: fn\\n")
for f in ["/kaggle/working/tts/qwen_tts/core/tokenizer_25hz/modeling_qwen3_tts_tokenizer_v1.py",
          "/kaggle/working/tts/qwen_tts/core/tokenizer_12hz/modeling_qwen3_tts_tokenizer_v2.py"]:
    code = open(f).read()
    if "from transformers.utils import ModelOutput, auto_docstring, logging" in code:
        open(f, "w").write(code.replace(
            "from transformers.utils import ModelOutput, auto_docstring, logging", noop))

if not os.path.exists("/usr/local/bin/cloudflared"):
    subprocess.run(["wget", "-q",
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        "-O", "/usr/local/bin/cloudflared"])
    subprocess.run(["chmod", "+x", "/usr/local/bin/cloudflared"])

subprocess.run(["pkill", "-f", "uvicorn"], capture_output=True)
time.sleep(2)

env = {**os.environ,
       "TTS_BACKEND": "official",
       "TTS_MODEL_NAME": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"}
log = open("/kaggle/working/server.log", "w")
subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app",
    "--host", "0.0.0.0", "--port", "8880"],
    cwd="/kaggle/working/tts", env=env, stdout=log, stderr=log)

cf_log = open("/kaggle/working/cf.log", "w")
subprocess.Popen(["/usr/local/bin/cloudflared", "tunnel", "--url", "http://localhost:8880"],
    stderr=cf_log, stdout=subprocess.DEVNULL)

# 5. cloudflare 폴링 (90초 고정 → 최대 2분 폴링)
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
    print("Railway 등록:", r.status_code, r.json())
    # 모델 자동 로딩 대기 (최대 10분)
    print("모델 자동 로딩 대기 중... /health 폴링")
    for _w in range(200):
        time.sleep(3)
        try:
            hr = requests.get(f"{tunnel_url}/health", timeout=10)
            if hr.status_code == 200:
                info = hr.json()
                if info.get("backend", {}).get("ready", False):
                    print(f"✅ 모델 로딩 완료! ({_w * 3}초)")
                    break
                else:
                    if _w % 10 == 0:
                        print(f"모델 로딩 중... ({_w * 3}초 경과)")
        except Exception as e:
            if _w % 10 == 0:
                print(f"서버 대기중: {e}")
else:
    print("터널 URL을 찾지 못했습니다.")

# 커널 살아있는 동안 서버 유지 + 5분마다 URL 재등록 (Railway 재시작 대응)
print("서버 유지 중... (최대 9시간)")
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
        # 디버그: 실제 쓰여진 메타데이터 확인
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
        return JSONResponse(status_code=403, content={"status": "error", "message": "권한 없음"})
    global _kaggle_start_time
    if not _GITHUB_TOKEN:
        return JSONResponse(status_code=503, content={"status": "error", "message": "GITHUB_TOKEN 미설정"})

    _kaggle_start_time = time.time()
    script = KAGGLE_SETUP_SCRIPT.replace("__GITHUB_TOKEN__", _GITHUB_TOKEN).replace("__RAILWAY_URL__", _RAILWAY_URL)

    try:
        rc, out = await asyncio.get_event_loop().run_in_executor(None, _kaggle_push_sync, script)
        logger.info(f"kaggle push rc={rc} out={out[:200]}")
        if rc == 0:
            return {"status": "ok", "message": "Kaggle 노트북 실행 시작됨"}
        else:
            return JSONResponse(status_code=500, content={"status": "error", "message": f"kaggle push 실패: {out[:300]}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/kaggle-status")
async def get_kaggle_status():
    # push 이후에 등록된 URL만 ready로 반환 (이전 세션 URL 무시)
    if _kaggle_tts_url and _kaggle_url_registered_at >= _kaggle_start_time:
        return {"status": "ready", "url": _kaggle_tts_url, "message": "✅ 서버 준비 완료!"}
    try:
        out = await asyncio.get_event_loop().run_in_executor(None, _kaggle_status_sync)
        logger.info(f"kaggle status: {out[:100]}")
        if "running" in out.lower():
            return {"status": "running", "message": "🔄 서버 시작 중... (약 2~3분 소요)"}
        elif "queued" in out.lower():
            return {"status": "queued", "message": "⏳ GPU 할당 대기 중..."}
        elif "complete" in out.lower():
            return {"status": "complete", "message": "완료 (터널 등록 대기 중...)"}
        elif "error" in out.lower():
            return {"status": "error", "message": f"❌ 오류: {out[:100]}"}
    except Exception as e:
        logger.warning(f"Kaggle 상태 조회 실패: {e}")
    return {"status": "idle", "message": ""}

@app.post("/api/stop-kaggle")
async def stop_kaggle_server(req: Request):
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    master_email = os.environ.get("MASTER_EMAIL", "master@gmail.com")
    if data.get("email", "").lower() != master_email.lower():
        return JSONResponse(status_code=403, content={"status": "error", "message": "권한 없음"})
    global _kaggle_tts_url
    try:
        import subprocess as sp
        _setup_kaggle_env()
        sp.run(["kaggle", "kernels", "cancel", f"{_KAGGLE_USERNAME}/{_KAGGLE_KERNEL_SLUG}"],
               capture_output=True, timeout=30)
        _kaggle_tts_url = ""
        return {"status": "ok", "message": "서버 중지 요청됨"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

class EmailCheckReq(BaseModel):
    email: str
    password: str

@app.post("/api/check-access")
async def check_access(req: EmailCheckReq):
    # USERS 형식: "email1:pass1,email2:pass2" (개별 비밀번호)
    users_env = os.environ.get("USERS", "")
    if users_env:
        user_map = {}
        for entry in users_env.split(","):
            entry = entry.strip()
            if ":" in entry:
                e, p = entry.split(":", 1)
                user_map[e.strip().lower()] = p.strip()
        if not user_map:
            return JSONResponse(status_code=500, content={"status": "error", "message": "서버 설정 오류"})
        stored = user_map.get(req.email.strip().lower())
        if stored and req.password == stored:
            return {"status": "ok"}
        return JSONResponse(status_code=401, content={"status": "error", "message": "이메일 또는 비밀번호가 올바르지 않습니다"})
    # 하위 호환: ALLOWED_EMAILS + ACCESS_PASSWORD
    allowed = os.environ.get("ALLOWED_EMAILS", "")
    password = os.environ.get("ACCESS_PASSWORD", "")
    allowed_list = [e.strip().lower() for e in allowed.split(",") if e.strip()]
    if not allowed_list or not password:
        return JSONResponse(status_code=500, content={"status": "error", "message": "서버 설정 오류"})
    if req.email.strip().lower() in allowed_list and req.password == password:
        return {"status": "ok"}
    return JSONResponse(status_code=401, content={"status": "error", "message": "이메일 또는 비밀번호가 올바르지 않습니다"})

@app.get("/api/progress")
async def api_progress():
    from renderer import render_progress
    return render_progress

@app.post("/api/render")
async def api_render(req: RenderRequest):
    logger.info(f"렌더링 요청: {len(req.scenes)}장면 | {req.w}x{req.h} @ {req.fps}fps | 폰트={req.subtitle_font} | 트랜지션={req.transition_type}")
    try:
        # Grok 동영상 씬에 실제 파일 경로 주입
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
        logger.info(f"렌더링 완료: {output_file}")
        return {"status": "success", "file": output_file}
    except Exception as e:
        logger.error(f"렌더링 실패: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

_whisper_model = None

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        # PyInstaller 번들: _MEIPASS를 PATH에 추가해 whisper가 ffmpeg 찾도록 보장
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
    """Whisper로 음성 파일을 분석하여 단어별 타이밍 정보 반환"""
    try:
        import whisper as _whisper
    except ImportError:
        return JSONResponse(status_code=503, content={"status": "error", "message": "Whisper 미설치 (로컬 서버 전용 기능)"})

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    try:
        logger.info(f"음성 분석 시작: {file.filename}")
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
        logger.info(f"음성 분석 완료: {len(timings)}개 단어 감지")
        return {"status": "success", "text": full_text, "timings": timings, "duration": result.get("duration", 0)}
    except Exception as e:
        logger.error(f"음성 분석 실패: {e}", exc_info=True)
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
    """WebCodecs 렌더링 결과를 스트리밍으로 받아 outputs에 저장"""
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
        logger.error(f"save-video-upload 오류: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/output-path/{filename}")
async def get_output_path(filename: str):
    file_path = os.path.join("outputs", filename)
    abs_path = os.path.abspath(file_path)
    if os.path.exists(abs_path):
        return {"status": "ok", "path": abs_path}
    return JSONResponse(status_code=404, content={"status": "error", "message": "파일 없음"})

@app.post("/api/save-audio")
async def save_audio_named(request: Request):
    import base64 as _b64
    data = await request.json()
    b64 = data.get("data", "")
    filename = data.get("filename", f"audio_{int(time.time())}.wav")
    if not b64:
        return JSONResponse(status_code=400, content={"status": "error", "message": "데이터 없음"})
    try:
        os.makedirs("outputs", exist_ok=True)
        file_path = os.path.join("outputs", filename)
        with open(file_path, "wb") as f:
            f.write(_b64.b64decode(b64))
        return {"status": "ok", "filename": filename, "url": f"/download-audio/{filename}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/download-audio/{filename}")
async def download_audio_file(filename: str):
    file_path = os.path.join("outputs", filename)
    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    ext = filename.rsplit(".", 1)[-1].lower()
    media_type = "audio/wav" if ext == "wav" else "audio/mpeg" if ext == "mp3" else "application/octet-stream"
    return FileResponse(path=file_path, filename=filename, media_type=media_type)

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
        logger.error(f"upload-audio 오류: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/save-image")
async def save_image(request: Request):
    """base64 이미지를 outputs 폴더에 저장 후 다운로드 URL 반환"""
    data = await request.json()
    b64 = data.get("data", "")
    ext = data.get("ext", "png")
    filename = data.get("filename", f"image_{int(time.time())}.{ext}")
    if not b64:
        return JSONResponse(status_code=400, content={"status": "error", "message": "데이터 없음"})
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
    """여러 이미지를 outputs 폴더에 저장하고 ZIP으로 묶어서 경로 반환"""
    import base64 as _b64
    import zipfile
    data = await request.json()
    images = data.get("images", [])        # [{filename, data, ext}, ...] — base64
    server_files = data.get("server_files", [])  # [{zipName, filename}, ...] — 서버 저장 파일
    if not images and not server_files:
        return JSONResponse(status_code=400, content={"status": "error", "message": "이미지 없음"})
    try:
        os.makedirs("outputs", exist_ok=True)
        zip_name = f"images_{int(time.time())}.zip"
        zip_path = os.path.join("outputs", zip_name)
        saved = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            # base64 이미지
            for img in images:
                fn = img.get("filename", f"scene_{int(time.time())}.png")
                b64 = img.get("data", "")
                if not b64:
                    continue
                zf.writestr(fn, _b64.b64decode(b64))
                saved.append(fn)
            # 서버 저장 파일
            for sf in server_files:
                src = os.path.join("outputs", sf.get("filename", ""))
                zip_entry = sf.get("zipName", sf.get("filename", ""))
                if src and os.path.exists(src):
                    zf.write(src, zip_entry)
                    saved.append(zip_entry)
        if not saved:
            return JSONResponse(status_code=400, content={"status": "error", "message": "이미지 없음"})
        return {"status": "ok", "zip": zip_name, "count": len(saved), "path": f"outputs/{zip_name}"}
    except Exception as e:
        logger.error(f"save-all-images 오류: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/youtube-transcripts")
async def youtube_transcripts(request: Request):
    """유튜브 채널 URL → 최근 영상 2~3개 자막 자동 수집"""
    import re
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
    except ImportError:
        return JSONResponse(status_code=500, content={"status": "error", "message": "youtube-transcript-api 미설치"})

    data = await request.json()
    channel_url = data.get("channel_url", "").strip()
    video_urls  = data.get("video_urls", [])   # 개별 영상 URL 목록 (채널 대신 직접 지정 가능)

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

    # 영상 URL 목록이 직접 주어진 경우
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
            return JSONResponse(status_code=404, content={"status": "error", "message": "자막을 가져올 수 없습니다. 자막이 비활성화된 영상이거나 지원하지 않는 형식입니다."})
        return {"status": "ok", "transcripts": results}

    # 채널 URL에서 최근 영상 ID 추출 (yt-dlp 없이 간단 스크래핑)
    if not channel_url:
        return JSONResponse(status_code=400, content={"status": "error", "message": "channel_url 또는 video_urls 필요"})

    try:
        import urllib.request
        import html
        # 채널 페이지 HTML에서 영상 ID 추출
        req = urllib.request.Request(channel_url + "/videos", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="ignore")
        vids = list(dict.fromkeys(re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', page)))[:5]
        if not vids:
            return JSONResponse(status_code=404, content={"status": "error", "message": "채널에서 영상을 찾을 수 없습니다. 영상 URL을 직접 입력해주세요."})
        results = []
        for vid in vids:
            txt = fetch_transcript(vid)
            if txt:
                results.append({"video_id": vid, "url": f"https://youtu.be/{vid}", "transcript": txt})
            if len(results) >= 3:
                break
        if not results:
            return JSONResponse(status_code=404, content={"status": "error", "message": "자막이 있는 영상을 찾지 못했습니다. 영상 URL을 직접 입력해주세요."})
        return {"status": "ok", "transcripts": results}
    except Exception as e:
        logger.error(f"youtube-transcripts 오류: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


# ── GrokBridge API ──

@app.get("/api/grok/status")
async def grok_status():
    """GrokBridge 연결 상태 반환"""
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
    """GrokBridge WebSocket 서버 시작 (포트 9876)"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    try:
        if not _grok_bridge.is_running:
            await _grok_bridge.start()
        return {"status": "ok", "running": _grok_bridge.is_running}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/grok/open-chrome")
async def grok_open_chrome():
    """Chrome을 열고 grok.com으로 이동"""
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
        return JSONResponse(status_code=503, content={"status": "error", "message": "Chrome 자동 실행은 Windows 전용 기능입니다."})

@app.post("/api/grok/stop")
async def grok_stop():
    """GrokBridge WebSocket 서버 중지"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    try:
        await _grok_bridge.stop()
        return {"status": "ok", "running": _grok_bridge.is_running}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

class GrokSetFolderReq(BaseModel):
    download_folder: str

@app.post("/api/grok/set-folder")
async def grok_set_folder(req: GrokSetFolderReq):
    """다운로드 폴더 설정"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    try:
        folder = req.download_folder.strip()
        if not folder:
            return JSONResponse(status_code=400, content={"status": "error", "message": "폴더 경로 필요"})
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
    """씬 작업 목록을 MakeLensAuto 확장프로그램으로 전송"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    if not _grok_bridge.is_running:
        return JSONResponse(status_code=400, content={"status": "error", "message": "GrokBridge 서버가 실행 중이 아닙니다. 먼저 서버를 시작해주세요."})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "MakeLensAuto 확장프로그램이 연결되지 않았습니다."})
    try:
        global _grok_session_start_time, _grok_completed_videos, _grok_task_cache
        # 새 세션 시작 전 이전 작업 취소 (확장앱 대기열 초기화)
        await _grok_bridge.cancel_all_tasks()
        
        import asyncio
        await asyncio.sleep(0.5)  # 확장앱이 CANCEL_ALL을 처리하고 큐를 비울 시간을 약간 줌

        # 이전 완료 목록 초기화 + 현재 시각 기록
        _grok_completed_videos = {}
        _grok_session_start_time = time.time()

        # 다운로드 폴더 설정
        if req.download_folder:
            folder = req.download_folder.strip()
            os.makedirs(folder, exist_ok=True)
            _grok_bridge.download_folder = folder
            await _grok_bridge.set_project_path(folder, folder)

        # 배치 모드 설정: 실패해도 다음 작업 계속 진행
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

        # 캐시에 저장하여 재실행 요청 시 사용
        for t in tasks_info:
            _grok_task_cache[t["sceneNo"]] = t

        # 사이드패널에 전체 목록 미리보기 먼저 전송
        preview_info = [{"sceneNo": t["sceneNo"], "prompt": t["prompt"], "folderName": ""} for t in tasks_info]
        await _grok_bridge.send_queue_preview(preview_info, is_new_run=True)

        await _grok_bridge.add_all_tasks(tasks_info)
        return {"status": "ok", "sent": len(tasks_info)}
    except Exception as e:
        logger.error(f"grok/send-tasks 오류: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/grok/completed")
async def grok_completed():
    """완료된 Grok 영상 목록 반환 (프론트엔드가 폴링하여 씬에 적용)"""
    import re

    # 세션 시작 후 지정 폴더에 생성된 scene_XX_grok.mp4 파일만 스캔
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
        result.append({"sceneNo": scene_no, "videoUrl": f"/api/grok/video/{scene_no}"})
    failed = [{"sceneNo": sn, "error": msg} for sn, msg in list(_grok_failed_scenes.items())]
    return {"completed": result, "failed": failed}

@app.get("/api/grok/video/{scene_no}")
async def grok_video_file(scene_no: int):
    """씬 번호로 Grok 생성 영상 파일 제공"""
    video_path = _grok_completed_videos.get(scene_no)
    if not video_path or not os.path.exists(video_path):
        return JSONResponse(status_code=404, content={"error": "영상 없음"})
    return FileResponse(path=video_path, media_type="video/mp4",
                        filename=os.path.basename(video_path))

@app.post("/api/grok/cancel")
async def grok_cancel():
    """전체 작업 취소"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    try:
        await _grok_bridge.cancel_all_tasks()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/api/grok/resume")
async def grok_resume():
    """실패로 멈춘 대기열 이어서 실행"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "확장프로그램이 연결되지 않았습니다."})
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
    """확장앱 런타임 설정 변경"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "확장프로그램이 연결되지 않았습니다."})
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
    """특정 씬 재실행"""
    if not _grok_available or not _grok_bridge:
        return JSONResponse(status_code=500, content={"status": "error", "message": "grok_bridge 없음"})
    if not _grok_bridge.is_connected:
        return JSONResponse(status_code=400, content={"status": "error", "message": "확장프로그램이 연결되지 않았습니다."})
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
        return JSONResponse({"ok": False, "error": "Qwen3-TTS는 로컬 Windows 전용 기능입니다."}, status_code=503)
    bat_path = _qwen_bat_path()
    if not bat_path:
        return JSONResponse({"ok": False, "error": "Qwen3-TTS 런처를 찾을 수 없습니다."}, status_code=404)
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
            logger.info("[Whisper] large-v3 CUDA 로드 완료")
        except Exception:
            _whisper_model = WhisperModel("large-v3", device="cpu", compute_type="int8")
            logger.info("[Whisper] large-v3 CPU 로드 완료")
    return _whisper_model

@app.post("/api/whisper-align")
async def whisper_align(request: Request):
    try:
        from faster_whisper import WhisperModel as _WM
    except ImportError:
        return JSONResponse(status_code=503, content={"status": "error", "message": "faster-whisper 미설치 (로컬 서버 전용 기능)"})
    data = await request.json()
    audio_url = data.get("audio_url", "")
    if not audio_url:
        return JSONResponse(status_code=400, content={"status": "error", "message": "audio_url 없음"})
    # URL → 파일 경로 변환
    if audio_url.startswith("/download-audio/"):
        filename = audio_url.replace("/download-audio/", "")
        audio_path = os.path.join("outputs", filename)
    else:
        return JSONResponse(status_code=400, content={"status": "error", "message": "지원하지 않는 URL 형식"})
    if not os.path.exists(audio_path):
        return JSONResponse(status_code=404, content={"status": "error", "message": f"파일 없음: {audio_path}"})
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
        logger.error(f"[Whisper] 오류: {e}")
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
                        print("[ScriptStudio] GrokBridge WebSocket 서버 시작: ws://localhost:9876")
                    except Exception as _be:
                        print(f"[ScriptStudio] GrokBridge 시작 실패 (무시): {_be}")
                config = uvicorn.Config(app, host="127.0.0.1", port=8000, reload=False)
                server = uvicorn.Server(config)
                await server.serve()
            asyncio.run(_start_with_bridge())

        t = threading.Thread(target=_run_server, daemon=True)
        t.start()
        time.sleep(1.5)  # 서버 기동 대기

        webview.create_window(
            "ScriptStudio",
            "http://127.0.0.1:8000",
            width=1440, height=900,
            resizable=True,
            min_size=(900, 600),
        )
        webview.start()

    except ImportError:
        # pywebview 미설치 시 브라우저로 폴백
        import webbrowser
        def _open_browser():
            time.sleep(1.2)
            webbrowser.open("http://localhost:8000")
        threading.Thread(target=_open_browser, daemon=True).start()
        uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
