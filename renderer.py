import os
import base64
import binascii
import asyncio
import subprocess
from typing import Any
import uuid
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

# Windows에서 subprocess 콘솔 창 숨김
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_executor = ThreadPoolExecutor(max_workers=2)

logger = logging.getLogger(__name__)

render_progress = {
    "status": "idle",
    "completed": 0,
    "total": 0,
}

# FFmpeg 경로: PyInstaller 번들 → 프로젝트 폴더 → 시스템 설치 순으로 탐색
def _find_ffmpeg() -> str:
    import sys
    _base = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if sys.platform == "win32":
        if getattr(sys, "frozen", False):
            _meipass = getattr(sys, "_MEIPASS", "")
            if _meipass:
                candidates.append(os.path.join(_meipass, "ffmpeg.exe"))
            candidates.append(os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"))
        candidates += [
            os.path.join(_base, "ffmpeg-master-latest-win64-gpl", "bin", "ffmpeg.exe"),
            os.path.join(_base, "ffmpeg.exe"),
            os.path.join(_base, "ffmpeg", "bin", "ffmpeg.exe"),
            r"C:\ffmpeg\bin\ffmpeg.exe",
        ]
    candidates.append("ffmpeg")  # PATH fallback (Linux/Mac/Windows)
    for p in candidates:
        if p == "ffmpeg" or os.path.isfile(p):
            logger.info(f"FFmpeg 사용 경로: {p}")
            return p
    raise RuntimeError("FFmpeg를 찾을 수 없습니다.")

FFMPEG_PATH = _find_ffmpeg()

# 실제 시스템에 장착된 물리적 GPU를 확인하여 최적의 H.264 인코더 반환 (NVIDIA, AMD, Intel)
def _get_available_hw_encoder() -> str | None:
    gpu_vendor = None
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, text=True,
                creationflags=_NO_WINDOW
            )
            output = result.stdout.lower()
            if "nvidia" in output:
                gpu_vendor = "nvenc"
            elif "amd" in output or "radeon" in output:
                gpu_vendor = "amf"
            elif "intel" in output:
                gpu_vendor = "qsv"
        else:
            # Linux: nvidia-smi로 GPU 확인
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, text=True
                )
                if result.returncode == 0 and result.stdout.strip():
                    gpu_vendor = "nvenc"
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"GPU 물리 하드웨어 감지 실패: {e}")

    # 2. FFmpeg가 해당 하드웨어 인코더 모듈을 지원하는지 교차 검증
    try:
        enc_result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            creationflags=_NO_WINDOW
        )
        enc_output = enc_result.stdout
        
        if gpu_vendor == "nvenc" and b"h264_nvenc" in enc_output:
            return "nvenc"
        elif gpu_vendor == "amf" and b"h264_amf" in enc_output:
            return "amf"
        elif gpu_vendor == "qsv" and b"h264_qsv" in enc_output:
            return "qsv"
            
    except Exception:
        pass

    return None

HW_ENCODER = _get_available_hw_encoder()

if HW_ENCODER == "nvenc":
    logger.info("GPU 인코더(NVIDIA h264_nvenc) 사용 가능")
elif HW_ENCODER == "amf":
    logger.info("GPU 인코더(AMD h264_amf) 사용 가능")
elif HW_ENCODER == "qsv":
    logger.info("GPU 인코더(Intel h264_qsv) 사용 가능")
else:
    logger.info("GPU 인코더 감지 실패 → libx264(CPU) 사용")

def get_video_encoder():
    """GPU 벤더별 가용 여부에 따라 인코더 옵션 반환"""
    if HW_ENCODER == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4"]
    elif HW_ENCODER == "amf":
        return ["-c:v", "h264_amf", "-quality", "speed"]
    elif HW_ENCODER == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "fast"]
    else:
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]

_DEFAULT_FONT = "Noto Sans CJK KR" if sys.platform != "win32" else "맑은 고딕"

# Windows 전용 한글 폰트 → Linux Noto 매핑 (Railway 서버가 Linux이므로 필요)
_WIN_FONT_MAP = {
    "맑은 고딕": "Noto Sans CJK KR",
    "굴림": "Noto Sans CJK KR",
    "돋움": "Noto Sans CJK KR",
    "바탕": "Noto Serif CJK KR",
    "궁서": "Noto Serif CJK KR",
    "나눔고딕": "Noto Sans CJK KR",
    "나눔바탕": "Noto Serif CJK KR",
}

def _resolve_font(font: str) -> str:
    if sys.platform == "win32":
        return font or _DEFAULT_FONT
    return _WIN_FONT_MAP.get(font, font) or _DEFAULT_FONT

def create_ass_subtitle(text_chunks, duration, w, h, fontname=None, fontcolor="&H00FFFFFF", bgstyle="box", fontsize=45, subtitle_timings=None, position="bottom"):
    fontname = _resolve_font(fontname or _DEFAULT_FONT)
    if bgstyle == "shadow":
        border_style, outline, shadow, outline_color, back_color = 1, 1.5, 2.5, "&H99000000", "&H00000000"
    elif bgstyle == "outline":
        border_style, outline, shadow, outline_color, back_color = 1, 15, 0, "&H00000000", "&H00000000"
    else:  # box
        border_style, outline, shadow, outline_color, back_color = 3, 15, 0, "&H80000000", "&H00000000"

    # ASS Alignment: 2=하단중앙, 5=화면중앙, 8=상단중앙
    pos_align = {'bottom': 2, 'center': 5, 'top': 8}.get(position, 2)
    pos_marginv = {'bottom': 80, 'center': 0, 'top': 80}.get(position, 80)

    ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{fontname},{fontsize},{fontcolor},&H000000FF,{outline_color},{back_color},-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{pos_align},20,20,{pos_marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def chunk_weight(c):
        base = len(c.replace(" ", "")) or 1
        # 문장 부호 뒤 자연스러운 멈춤을 가중치로 반영
        if c.rstrip().endswith(('.', '!', '?', '…')):
            base += 3
        elif c.rstrip().endswith((',', ';', ':')):
            base += 1
        return base

    if subtitle_timings:
        # JS에서 계산된 정확한 타임스탬프 사용
        for t in subtitle_timings:
            start_t = format_ass_time(float(t['start']))
            end_t   = format_ass_time(float(t['end']))
            text    = str(t['text']).replace('\n', '\\N').replace('"', '').replace("'", '')
            ass_content += f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{text}\n"
    else:
        # 폴백: 글자 수 가중치 기반 추정
        total_len = sum(chunk_weight(c) for c in text_chunks) or 1
        current_time = 0.0
        for chunk in text_chunks:
            c_len = chunk_weight(chunk)
            chunk_dur = (c_len / total_len) * duration
            start_t = format_ass_time(current_time)
            current_time += chunk_dur
            end_t = format_ass_time(current_time)
            text = chunk.replace('\n', '\\N').replace('"', '').replace("'", '')
            ass_content += f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{text}\n"

    return ass_content

def format_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"

def _run_ffmpeg_sync(cmd: list) -> None:
    """FFmpeg 동기 실행 (스레드에서 호출됨)"""
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    if result.returncode != 0:
        err_msg = result.stderr.decode('utf-8', errors='replace')[-2000:]
        logger.error(f"FFmpeg 오류 (code={result.returncode}):\n{err_msg}")
        raise RuntimeError(f"FFmpeg 실패 (코드 {result.returncode}): {err_msg[-500:]}")

async def run_ffmpeg(cmd: list) -> None:
    """FFmpeg을 스레드 풀에서 실행 (서버 블로킹 방지, PyInstaller 호환)"""
    logger.info(f"FFmpeg 실행: {' '.join(str(c) for c in cmd[:6])}...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _run_ffmpeg_sync, cmd)

def _safe_path_exists(path_str: str) -> bool:
    try:
        return bool(path_str) and os.path.exists(path_str)
    except (OSError, ValueError):
        return False

async def _render_zoom_ffmpeg(img_path, w, h, dur, fps, audio_path, ass_path_escaped, clip_path, show_subtitle=True, zoom_speed_value=1.08, zoom_type='zoom_in'):
    """FFmpeg zoompan 필터로 줌/패닝 (Linux/Railway용, 메모리 효율적)"""
    total_frames = int(dur * fps)
    z_expr = {
        'zoom_in':  f"min(zoom+{(zoom_speed_value-1)/total_frames:.6f},{zoom_speed_value})",
        'zoom_out': f"max(zoom-{(zoom_speed_value-1)/total_frames:.6f},1.0)",
    }.get(zoom_type, "1.0")

    if zoom_type in ('pan_left', 'pan_right', 'pan_up', 'pan_down'):
        pan_scale = zoom_speed_value
        if zoom_type == 'pan_left':
            x_expr = f"iw*(1-1/{pan_scale})*on/{total_frames}"
            y_expr = f"(ih-oh)/2"
        elif zoom_type == 'pan_right':
            x_expr = f"iw*(1-1/{pan_scale})*(1-on/{total_frames})"
            y_expr = f"(ih-oh)/2"
        elif zoom_type == 'pan_up':
            x_expr = f"(iw-ow)/2"
            y_expr = f"ih*(1-1/{pan_scale})*on/{total_frames}"
        else:
            x_expr = f"(iw-ow)/2"
            y_expr = f"ih*(1-1/{pan_scale})*(1-on/{total_frames})"
        zoom_filter = f"zoompan=z={pan_scale}:x='{x_expr}':y='{y_expr}':d={total_frames}:s={w}x{h}:fps={fps}"
    elif zoom_type == 'none':
        zoom_filter = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    else:
        zoom_filter = f"zoompan=z='{z_expr}':x='(iw-ow)/2':y='(ih-oh)/2':d={total_frames}:s={w}x{h}:fps={fps}"

    encoder_opts = get_video_encoder()
    vf_parts = [zoom_filter]
    if show_subtitle and ass_path_escaped:
        vf_parts.append(f"ass={ass_path_escaped}")

    return [
        FFMPEG_PATH, "-y", "-loop", "1", "-i", img_path,
        "-i", audio_path,
        "-vf", ",".join(vf_parts),
        "-t", str(dur),
    ] + encoder_opts + [
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest", clip_path
    ]

def _render_zoom_pipe(img_path, w, h, dur, fps, audio_path, ass_path_escaped, clip_path, show_subtitle=True, zoom_speed_value=1.08, zoom_type='zoom_in'):
    """OpenCV로 프레임별 줌/패닝 생성 후 FFmpeg 파이프 인코딩 (초고속, 떨림 100% 제거)"""
    import cv2
    import numpy as np

    # 한글 경로 지원을 위해 numpy로 읽고 cv2로 디코드
    img_array = np.fromfile(img_path, np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"OpenCV 이미지 디코드 실패: {img_path}")

    # 원본 종횡비 유지하면서 w, h 캔버스 꽉 차게 리사이즈 후 중앙 크롭
    src_h, src_w = img.shape[:2]
    src_ratio = src_w / src_h
    out_ratio = w / h
    if src_ratio > out_ratio:
        new_h = h
        new_w = int(src_w * h / src_h)
    else:
        new_w = w
        new_h = int(src_h * w / src_w)

    img_resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    x_off = (new_w - w) // 2
    y_off = (new_h - h) // 2
    img_cropped = img_resized[y_off:y_off+h, x_off:x_off+w]

    # 패닝용 확대 이미지 미리 준비
    pan_scale = max(zoom_speed_value, 1.08)
    pan_w = int(w * pan_scale)
    pan_h = int(h * pan_scale)
    if zoom_type in ('pan_left', 'pan_right', 'pan_up', 'pan_down'):
        img_pan = cv2.resize(img_cropped, (pan_w, pan_h), interpolation=cv2.INTER_CUBIC)
        extra_x = pan_w - w
        extra_y = pan_h - h
    else:
        img_pan = None
        extra_x = extra_y = 0

    total_frames = max(int(dur * fps), 1)
    encoder_opts = get_video_encoder()

    vf_args = []
    if show_subtitle and ass_path_escaped:
        vf_args = ["-vf", f"ass={ass_path_escaped}"]

    cmd = [
        FFMPEG_PATH, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{w}x{h}", "-pix_fmt", "bgr24",  # OpenCV는 기본 BGR
        "-r", str(fps), "-i", "pipe:0",
        "-i", audio_path,
    ] + vf_args + encoder_opts + [
        "-c:a", "aac", "-b:a", "192k", "-b:v", "5M",
        "-pix_fmt", "yuv420p", "-t", str(dur),
        clip_path
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        creationflags=_NO_WINDOW
    )

    try:
        center = (w / 2, h / 2)
        for frame_i in range(total_frames):
            progress = frame_i / max(total_frames - 1, 1)

            if zoom_type == 'zoom_out':
                zoom = zoom_speed_value - (zoom_speed_value - 1.0) * progress
                M = cv2.getRotationMatrix2D(center, 0, zoom)
                frame = cv2.warpAffine(img_cropped, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            elif zoom_type == 'pan_left':
                x_start = int(progress * extra_x)
                y_start = extra_y // 2
                frame = img_pan[y_start:y_start+h, x_start:x_start+w]
            elif zoom_type == 'pan_right':
                x_start = int((1.0 - progress) * extra_x)
                y_start = extra_y // 2
                frame = img_pan[y_start:y_start+h, x_start:x_start+w]
            elif zoom_type == 'pan_up':
                x_start = extra_x // 2
                y_start = int(progress * extra_y)
                frame = img_pan[y_start:y_start+h, x_start:x_start+w]
            elif zoom_type == 'pan_down':
                x_start = extra_x // 2
                y_start = int((1.0 - progress) * extra_y)
                frame = img_pan[y_start:y_start+h, x_start:x_start+w]
            elif zoom_type == 'none':
                frame = img_cropped
            else:  # zoom_in (기본)
                zoom = 1.0 + (zoom_speed_value - 1.0) * progress
                M = cv2.getRotationMatrix2D(center, 0, zoom)
                frame = cv2.warpAffine(img_cropped, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

            proc.stdin.write(frame.tobytes())
    except Exception as e:
        logger.error(f"OpenCV 프레임 생성 오류: {e}")
    finally:
        if proc.stdin:
            proc.stdin.close()

    _, stderr = proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode('utf-8', errors='replace')[-500:]
        logger.error(f"OpenCV 줌 파이프 FFmpeg 오류: {err_msg}")
        raise RuntimeError(f"FFmpeg 실패 (코드 {proc.returncode}): {err_msg}")

async def render_video(req: Any) -> str:
    global render_progress
    render_id = uuid.uuid4().hex[:8]
    temp_dir = os.path.join("outputs", "temp", f"render_{render_id}")
    os.makedirs(temp_dir, exist_ok=True)

    w, h = req.w, req.h
    output_filename = f"final_output_{render_id}.mp4"
    output_path = os.path.join("outputs", output_filename)

    clips = []
    scene_clips = []  # (clip_path, actual_dur) — crossfade용
    render_progress["status"] = "rendering"
    render_progress["completed"] = 0
    render_progress["total"] = len(req.scenes)

    for i, scene in enumerate(req.scenes):
        dur = scene.dur
        scene_dir = os.path.join(temp_dir, f"scene_{i}")
        os.makedirs(scene_dir, exist_ok=True)

        img_path = os.path.join(scene_dir, "image.jpg")
        audio_path = os.path.join(scene_dir, "audio.wav")
        ass_path = os.path.join(scene_dir, "subtitles.ass")
        clip_path = os.path.join(scene_dir, "clip.mp4")

        logger.info(f"[{i+1}/{len(req.scenes)}] 장면 처리 중 (dur={dur:.1f}s)")

        # 1. 이미지/동영상 입력 준비
        grok_video_path = None
        scene_img_src = str(getattr(scene, "imgDataUrl", "") or "").strip()
        scene_video_src = str(getattr(scene, "grokVideoPath", "") or "").strip()
        scene_video_hint = bool(getattr(scene, "grokVideo", False))

        if scene_video_src and _safe_path_exists(scene_video_src):
            grok_video_path = scene_video_src
        elif scene_img_src:
            # 서버 저장 URL인 경우 파일 직접 복사
            if scene_img_src.startswith('/download-image/'):
                fname = scene_img_src.split('/download-image/')[-1]
                src = os.path.join("outputs", fname)
                if os.path.exists(src):
                    if fname.lower().endswith((".mp4", ".webm", ".mov", ".avi", ".mkv")):
                        grok_video_path = src
                    else:
                        import shutil
                        shutil.copy2(src, img_path)
                else:
                    await run_ffmpeg([FFMPEG_PATH, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", "-frames:v", "1", img_path])
            elif scene_img_src.startswith("data:video"):
                vid_data = scene_img_src.split(",", 1)[1] if "," in scene_img_src else ""
                if vid_data:
                    grok_video_path = os.path.join(scene_dir, "input_video.mp4")
                    try:
                        with open(grok_video_path, "wb") as f:
                            f.write(base64.b64decode(vid_data, validate=True))
                    except (binascii.Error, ValueError, OSError):
                        grok_video_path = None
                        await run_ffmpeg([FFMPEG_PATH, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", "-frames:v", "1", img_path])
                else:
                    await run_ffmpeg([FFMPEG_PATH, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", "-frames:v", "1", img_path])
            elif _safe_path_exists(scene_img_src) and os.path.isfile(scene_img_src):
                if scene_img_src.lower().endswith((".mp4", ".webm", ".mov", ".avi", ".mkv")):
                    grok_video_path = scene_img_src
                else:
                    import shutil
                    shutil.copy2(scene_img_src, img_path)
            elif scene_video_hint:
                # 브라우저 blob URL 등 서버에서 직접 읽을 수 없는 영상 입력은 블랙 프레임으로 폴백
                await run_ffmpeg([FFMPEG_PATH, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", "-frames:v", "1", img_path])
            else:
                img_data = scene_img_src.split(",", 1)[1] if "," in scene_img_src else scene_img_src
                try:
                    with open(img_path, "wb") as f:
                        f.write(base64.b64decode(img_data, validate=True))
                    from PIL import Image
                    with Image.open(img_path) as _img_test:
                        _img_test.verify()
                except Exception:
                    if os.path.exists(img_path):
                        os.remove(img_path)
                    await run_ffmpeg([FFMPEG_PATH, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", "-frames:v", "1", img_path])
        else:
            await run_ffmpeg([FFMPEG_PATH, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", "-frames:v", "1", img_path])

        # 2. 오디오 디코딩 (audioPath 우선, 없으면 base64 폴백)
        audio_src = getattr(scene, 'audioPath', None)
        if audio_src and os.path.exists(audio_src):
            import shutil as _shutil
            _shutil.copy2(audio_src, audio_path)
        elif scene.audioDataUrl:
            aud_data = scene.audioDataUrl.split(',')[1] if ',' in scene.audioDataUrl else scene.audioDataUrl
            with open(audio_path, "wb") as f:
                f.write(base64.b64decode(aud_data))
        else:
            await run_ffmpeg([FFMPEG_PATH, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(dur), audio_path])

        # 2-1. 실제 오디오 길이 측정 → dur 보정 (TTS 길이가 scene.dur와 다를 때 보정)
        import re as _re
        probe = subprocess.run(
            [FFMPEG_PATH, "-i", audio_path],
            stderr=subprocess.PIPE, stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW
        )
        m = _re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', probe.stderr.decode('utf-8', errors='replace'))
        if m:
            h_, mn_, s_ = m.groups()
            actual_dur = int(h_) * 3600 + int(mn_) * 60 + float(s_)
            if actual_dur > 0.1:
                if abs(actual_dur - dur) > 0.1:
                    logger.info(f"[{i+1}] 오디오 실제 길이({actual_dur:.2f}s) ≠ scene.dur({dur:.1f}s) → dur 보정")
                dur = actual_dur

        # 3. 자막(ASS) 생성 — show_subtitle=False 이면 스킵
        show_subtitle = getattr(req, 'show_subtitle', True)
        ass_path_escaped = None
        final_chunks = []
        subtitle_font = '맑은 고딕'

        if show_subtitle:
            import re

            def split_into_chunks(text, max_len=18):
                """구두점 → 공백(어절) → 강제 분할 순서로 자막 청크 생성"""
                # 1단계: 구두점 기준 분할
                parts = re.split(r'([.,!?。！？\n]+)', text)
                punct_chunks = []
                cur = ""
                for j, part in enumerate(parts):
                    cur += part
                    if re.search(r'[.,!?。！？\n]', part) or j == len(parts) - 1:
                        t = cur.strip()
                        if t:
                            punct_chunks.append(t)
                        cur = ""
                if not punct_chunks:
                    punct_chunks = [text]

                # 2단계: 청크가 너무 길면 공백(어절) 기준으로 추가 분할
                final = []
                for chunk in punct_chunks:
                    if len(chunk) <= max_len:
                        final.append(chunk)
                        continue
                    words = chunk.split(' ')
                    temp = ""
                    for w in words:
                        if len(temp) + len(w) > max_len and temp:
                            final.append(temp.strip())
                            temp = w + " "
                        else:
                            temp += w + " "
                    if temp.strip():
                        final.append(temp.strip())

                # 3단계: 여전히 긴 청크는 max_len 글자씩 강제 분할
                result = []
                for chunk in final:
                    if len(chunk) <= max_len:
                        result.append(chunk)
                    else:
                        for k in range(0, len(chunk), max_len):
                            result.append(chunk[k:k+max_len])
                return result if result else [text]

            final_chunks = split_into_chunks(scene.script)

            subtitle_font = getattr(req, 'subtitle_font', '맑은 고딕') or '맑은 고딕'
            if subtitle_font.strip().isdigit():
                subtitle_font = '맑은 고딕'

            subtitle_timings = getattr(scene, 'subtitleTimings', None)
            ass_content = create_ass_subtitle(
                final_chunks, dur, w, h,
                subtitle_font,
                getattr(req, 'subtitle_color', '&H00FFFFFF'),
                getattr(req, 'subtitle_bg', 'box'),
                getattr(req, 'subtitle_size', 45),
                subtitle_timings=subtitle_timings,
                position=getattr(req, 'subtitle_position', 'bottom')
            )
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass_content)
            ass_path_escaped = ass_path.replace('\\', '/').replace(':', '\\:')

        # 4. 클립 생성
        w_int = int(w)
        h_int = int(h)
        fps_int = int(req.fps)
        dur_frames = max(int(dur * fps_int), 1)

        encoder_opts = get_video_encoder()

        if grok_video_path:
            vf_parts = [
                f"scale={w_int}:{h_int}:force_original_aspect_ratio=increase:flags=lanczos",
                f"crop={w_int}:{h_int}",
            ]
            if show_subtitle and ass_path_escaped:
                vf_parts.append(f"ass={ass_path_escaped}")
            vf_str = ",".join(vf_parts)
            cmd = [
                FFMPEG_PATH, "-y",
                "-stream_loop", "-1", "-i", grok_video_path,
                "-i", audio_path,
                "-vf", vf_str,
                "-map", "0:v:0", "-map", "1:a:0",
            ] + encoder_opts + [
                "-c:a", "aac", "-b:a", "192k", "-b:v", "5M",
                "-pix_fmt", "yuv420p", "-t", str(dur),
                clip_path
            ]
            logger.info("FFmpeg 동영상 클립 명령어: " + " ".join(str(c) for c in cmd))
            await run_ffmpeg(cmd)
        elif getattr(req, 'use_zoompan', True) and sys.platform == "win32":
            # 줌/패닝: Windows 로컬에서만 (OpenCV, 메모리 충분할 때)
            zoom_speed_val = getattr(req, 'zoom_speed', 1.08)
            scene_zoom_type = getattr(scene, 'zoom_type', None) or getattr(scene, 'motionEffect', None) or getattr(req, 'global_zoom_type', 'zoom_in')
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _render_zoom_pipe, img_path, w_int, h_int, dur, fps_int, audio_path, ass_path_escaped, clip_path, show_subtitle, zoom_speed_val, scene_zoom_type)
        else:
            vf_parts = [
                f"scale={w_int}:{h_int}:force_original_aspect_ratio=increase:flags=lanczos",
                f"crop={w_int}:{h_int}",
            ]
            if show_subtitle and ass_path_escaped:
                vf_parts.append(f"ass={ass_path_escaped}")
            vf_str = ",".join(vf_parts)
            
            cmd = [
                FFMPEG_PATH, "-y",
                "-loop", "1", "-framerate", str(fps_int), "-i", img_path,
                "-i", audio_path,
                "-vf", vf_str,
            ] + encoder_opts + [
                "-c:a", "aac", "-b:a", "192k", "-b:v", "5M",
                "-pix_fmt", "yuv420p", "-t", str(dur),
                clip_path
            ]
            logger.info("FFmpeg 명령어: " + " ".join(str(c) for c in cmd))
            await run_ffmpeg(cmd)

        clips.append(clip_path)
        scene_clips.append((clip_path, dur))

        # 장면 간격 무음 클립 삽입 (마지막 장면 제외)
        gap_sec = getattr(scene, 'gapSec', 0.0) or 0.0
        if gap_sec > 0 and i < len(req.scenes) - 1:
            gap_path = os.path.join(scene_dir, "gap.mp4")

            # 이전 씬 마지막 자막을 gap 구간에도 이어서 표시
            gap_vf_parts = [f"scale={w_int}:{h_int}:force_original_aspect_ratio=increase:flags=lanczos",
                            f"crop={w_int}:{h_int}"]
            if show_subtitle and final_chunks:
                last_text = final_chunks[-1].replace('\n', r'\N').replace('"', '').replace("'", '')
                _sfont   = subtitle_font if 'subtitle_font' in dir() else '맑은 고딕'
                _ssize   = getattr(req, 'subtitle_size', 45)
                _scolor  = getattr(req, 'subtitle_color', '&H00FFFFFF')
                _bgstyle = getattr(req, 'subtitle_bg', 'box')
                if _bgstyle == "shadow":
                    _bs, _ol, _sh, _oc, _bc = 1, 1.5, 2.5, "&H99000000", "&H00000000"
                elif _bgstyle == "outline":
                    _bs, _ol, _sh, _oc, _bc = 1, 15, 0, "&H00000000", "&H00000000"
                else:
                    _bs, _ol, _sh, _oc, _bc = 3, 15, 0, "&H80000000", "&H00000000"
                gap_ass_content = (
                    f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w_int}\nPlayResY: {h_int}\n\n"
                    f"[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
                    f"Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                    f"MarginL, MarginR, MarginV, Encoding\n"
                    f"Style: Default,{_sfont},{_ssize},{_scolor},&H000000FF,{_oc},{_bc},-1,0,0,0,100,100,0,0,{_bs},{_ol},{_sh},2,20,20,80,1\n\n"
                    f"[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                    f"Dialogue: 0,0:00:00.00,{format_ass_time(gap_sec)},Default,,0,0,0,,{last_text}\n"
                )
                gap_ass_path = os.path.join(scene_dir, "gap.ass")
                with open(gap_ass_path, "w", encoding="utf-8") as f:
                    f.write(gap_ass_content)
                gap_ass_escaped = gap_ass_path.replace('\\', '/').replace(':', '\\:')
                gap_vf_parts.append(f"ass={gap_ass_escaped}")

            gap_cmd = [
                FFMPEG_PATH, "-y",
                "-f", "lavfi", "-i", f"color=c=black:s={w_int}x{h_int}:r={fps_int}",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(gap_sec),
                "-vf", ",".join(gap_vf_parts),
            ] + encoder_opts + [
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                gap_path
            ]
            await run_ffmpeg(gap_cmd)
            clips.append(gap_path)

        render_progress["completed"] = i + 1

    # 5. 클립 병합
    render_progress["status"] = "concatenating"
    if getattr(req, 'transition_type', 'hard') == 'crossfade' and len(scene_clips) > 1:
        # 크로스페이드는 scene 클립만 사용 (gap 클립 제외)
        inputs = []
        for c, _ in scene_clips:
            inputs.extend(["-i", c])

        filter_complex = ""
        v_out = "[0:v]"
        a_out = "[0:a]"
        offset = 0.0

        for i in range(1, len(scene_clips)):
            dur_prev = scene_clips[i-1][1]  # 실제 측정된 dur 사용
            offset += dur_prev - 0.5
            filter_complex += f"{v_out}[{i}:v]xfade=transition=fade:duration=0.5:offset={offset:.3f}[v{i}];"
            filter_complex += f"{a_out}[{i}:a]acrossfade=d=0.5[a{i}];"
            v_out = f"[v{i}]"
            a_out = f"[a{i}]"

        encoder_opts = get_video_encoder()
        cmd = [FFMPEG_PATH, "-y"] + inputs + [
            "-filter_complex", filter_complex,
            "-map", v_out, "-map", a_out,
        ] + encoder_opts + [
            "-c:a", "aac", "-b:a", "192k", "-b:v", "5M",
            "-pix_fmt", "yuv420p", output_path
        ]
        logger.info("FFmpeg 명령어: " + " ".join(str(c) for c in cmd))
        await run_ffmpeg(cmd)
    else:
        concat_file = os.path.join(temp_dir, "concat.txt")
        with open(concat_file, "w", encoding="utf-8") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")

        concat_cmd = [
            FFMPEG_PATH, "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            output_path
        ]
        print("FFmpeg 명령어:", " ".join(str(c) for c in concat_cmd))
        await run_ffmpeg(concat_cmd)

    render_progress["status"] = "done"
    logger.info(f"렌더링 완료: {output_path}")
    return output_filename


def build_scene_subtitle_preview(scene, req, h):
    import re
    script = getattr(scene, 'script', '') or ''
    if not script.strip():
        return []

    max_len = 18

    def split_into_chunks(text):
        parts = re.split(r'([.,!?。！？\n]+)', text)
        punct_chunks = []
        cur = ""
        for j, part in enumerate(parts):
            cur += part
            if re.search(r'[.,!?。！？\n]', part) or j == len(parts) - 1:
                t = cur.strip()
                if t:
                    punct_chunks.append(t)
                cur = ""
        if not punct_chunks:
            punct_chunks = [text]

        final = []
        for chunk in punct_chunks:
            if len(chunk) <= max_len:
                final.append(chunk)
                continue
            words = chunk.split(' ')
            temp = ""
            for w in words:
                if len(temp) + len(w) > max_len and temp:
                    final.append(temp.strip())
                    temp = w + " "
                else:
                    temp += w + " "
            if temp.strip():
                final.append(temp.strip())

        result = []
        for chunk in final:
            if len(chunk) <= max_len:
                result.append(chunk)
            else:
                for k in range(0, len(chunk), max_len):
                    result.append(chunk[k:k+max_len])
        return result if result else [text]

    return split_into_chunks(script)


def export_premiere_xml(req) -> dict:
    """RenderRequest를 받아 Premiere Pro 임포트용 FCP XML + SRT 파일을 생성하고 경로를 반환"""
    import xml.etree.ElementTree as ET

    fps = req.fps or 30
    w = req.w or 1080
    h = req.h or 1920

    def sec_to_frames(sec: float) -> int:
        return max(1, round(sec * fps))

    def path_to_url(p: str) -> str:
        if not p:
            return ""
        p = os.path.abspath(p)
        # Premiere Pro는 file://localhost/ 형식을 올바르게 인식 (file:/// 은 UNC로 오파싱됨)
        return "file://localhost/" + p.replace("\\", "/")

    # 씬별 duration 계산
    scene_frames = []
    for sc in req.scenes:
        dur = (sc.dur or 0) + (sc.gapSec or 0)
        scene_frames.append(sec_to_frames(dur))

    total_frames = sum(scene_frames)

    def make_rate(parent, timebase, ntsc="FALSE"):
        r = ET.SubElement(parent, "rate")
        ET.SubElement(r, "timebase").text = str(timebase)
        ET.SubElement(r, "ntsc").text = ntsc
        return r

    root = ET.Element("xmeml", version="5")
    seq = ET.SubElement(root, "sequence")
    ET.SubElement(seq, "name").text = "ScriptStudio Sequence"
    ET.SubElement(seq, "duration").text = str(total_frames)
    make_rate(seq, fps)

    # timecode (필수)
    tc = ET.SubElement(seq, "timecode")
    make_rate(tc, fps)
    ET.SubElement(tc, "string").text = "00:00:00:00"
    ET.SubElement(tc, "frame").text = "0"
    ET.SubElement(tc, "displayformat").text = "NDF"
    reel = ET.SubElement(tc, "reel")
    ET.SubElement(reel, "name")

    ET.SubElement(seq, "in").text = "0"
    ET.SubElement(seq, "out").text = str(total_frames)

    media = ET.SubElement(seq, "media")

    # ── 비디오 트랙 ──
    video_el = ET.SubElement(media, "video")
    v_format = ET.SubElement(video_el, "format")
    v_schar = ET.SubElement(v_format, "samplecharacteristics")
    make_rate(v_schar, fps)
    ET.SubElement(v_schar, "width").text = str(w)
    ET.SubElement(v_schar, "height").text = str(h)
    ET.SubElement(v_schar, "anamorphic").text = "FALSE"
    ET.SubElement(v_schar, "pixelaspectratio").text = "square"
    ET.SubElement(v_schar, "fielddominance").text = "none"

    v_track = ET.SubElement(video_el, "track")
    ET.SubElement(v_track, "enabled").text = "TRUE"
    ET.SubElement(v_track, "locked").text = "FALSE"

    # ── 오디오 트랙 ──
    audio_el = ET.SubElement(media, "audio")
    ET.SubElement(audio_el, "numOutputChannels").text = "2"
    a_format = ET.SubElement(audio_el, "format")
    a_schar = ET.SubElement(a_format, "samplecharacteristics")
    ET.SubElement(a_schar, "depth").text = "16"
    ET.SubElement(a_schar, "samplerate").text = "44100"
    a_track = ET.SubElement(audio_el, "track")
    ET.SubElement(a_track, "outputchannelindex").text = "1"
    ET.SubElement(a_track, "enabled").text = "TRUE"
    ET.SubElement(a_track, "locked").text = "FALSE"

    # outputs/ 폴더 미리 생성 — CWD 기준 (download 엔드포인트와 동일)
    out_dir = "outputs"
    os.makedirs(out_dir, exist_ok=True)

    def save_b64_image(b64_str: str, idx: int) -> str:
        try:
            ext = "png"
            if b64_str.startswith("data:"):
                header = b64_str.split(",", 1)[0]
                if "video/mp4" in header or "video/mpeg" in header:
                    ext = "mp4"
                elif "video/webm" in header:
                    ext = "webm"
                elif "image/jpeg" in header or "image/jpg" in header:
                    ext = "jpg"
                elif "image/gif" in header:
                    ext = "gif"
                elif "image/webp" in header:
                    ext = "webp"
            if ',' in b64_str:
                b64_str = b64_str.split(',', 1)[1]
            img_bytes = base64.b64decode(b64_str)
            img_path = os.path.join(out_dir, f"scene_{idx+1:03d}.{ext}")
            with open(img_path, 'wb') as f:
                f.write(img_bytes)
            return img_path
        except Exception as e:
            logger.warning(f"씬 {idx+1} 미디어 저장 실패: {e}")
            return ""

    cursor = 0
    for i, (sc, frames) in enumerate(zip(req.scenes, scene_frames)):
        clip_id = f"clipitem-{i+1}"
        file_id = f"file-{i+1}"

        # 비디오/이미지 미디어만 — 오디오 파일은 비디오 트랙에 넣지 않음
        media_path = sc.grokVideoPath or ""
        if not media_path and sc.imgDataUrl:
            if sc.imgDataUrl.startswith('data:'):
                media_path = save_b64_image(sc.imgDataUrl, i)
            elif '/download-image/' in sc.imgDataUrl:
                # 이미 서버에 저장된 파일 → 로컬 경로로 변환
                fname = sc.imgDataUrl.split('/download-image/')[-1].split('?')[0]
                candidate = os.path.abspath(os.path.join("outputs", fname))
                if os.path.exists(candidate):
                    media_path = candidate
        media_url = path_to_url(media_path) if media_path else ""

        # ── 비디오 클립 (이미지/영상 있을 때만) ──
        if media_url:
            clip = ET.SubElement(v_track, "clipitem", id=clip_id)
            ET.SubElement(clip, "name").text = f"Scene {i+1}"
            ET.SubElement(clip, "duration").text = str(frames)
            make_rate(clip, fps)
            ET.SubElement(clip, "in").text = "0"
            ET.SubElement(clip, "out").text = str(frames)
            ET.SubElement(clip, "start").text = str(cursor)
            ET.SubElement(clip, "end").text = str(cursor + frames)
            f_el = ET.SubElement(clip, "file", id=file_id)
            ET.SubElement(f_el, "name").text = os.path.basename(media_path)
            ET.SubElement(f_el, "pathurl").text = media_url
            make_rate(f_el, fps)
            ET.SubElement(f_el, "duration").text = str(frames)
            f_media = ET.SubElement(f_el, "media")
            f_video = ET.SubElement(f_media, "video")
            f_vsc = ET.SubElement(f_video, "samplecharacteristics")
            make_rate(f_vsc, fps)
            ET.SubElement(f_vsc, "width").text = str(w)
            ET.SubElement(f_vsc, "height").text = str(h)
            ET.SubElement(f_vsc, "anamorphic").text = "FALSE"
            ET.SubElement(f_vsc, "pixelaspectratio").text = "square"
            ET.SubElement(f_vsc, "fielddominance").text = "none"
            if sc.script:
                marker = ET.SubElement(clip, "marker")
                ET.SubElement(marker, "comment").text = sc.script[:100]
                ET.SubElement(marker, "in").text = "0"
                ET.SubElement(marker, "out").text = str(frames)

            # ── 줌인 모션 (XML 줌인 체크 시) ──
            if getattr(req, 'use_zoompan', True):
                zoom_end = int(getattr(req, 'zoom_speed', 1.08) * 100)
                motion = ET.SubElement(clip, "motion")
                make_rate(motion, fps)
                m_start = ET.SubElement(motion, "start")
                ET.SubElement(m_start, "horiz").text = "0"
                ET.SubElement(m_start, "vert").text = "0"
                ET.SubElement(m_start, "scale").text = "100"
                ET.SubElement(m_start, "rotation").text = "0"
                ET.SubElement(m_start, "mix").text = "100"
                ET.SubElement(m_start, "scaletype").text = "zoom"
                m_end = ET.SubElement(motion, "end")
                ET.SubElement(m_end, "horiz").text = "0"
                ET.SubElement(m_end, "vert").text = "0"
                ET.SubElement(m_end, "scale").text = str(zoom_end)
                ET.SubElement(m_end, "rotation").text = "0"
                ET.SubElement(m_end, "mix").text = "100"
                ET.SubElement(m_end, "scaletype").text = "zoom"

        # ── 오디오 클립 (audioPath 있을 때만) ──
        if sc.audioPath and os.path.exists(sc.audioPath):
            a_clip = ET.SubElement(a_track, "clipitem", id=f"audio-{i+1}")
            ET.SubElement(a_clip, "name").text = f"Audio {i+1}"
            ET.SubElement(a_clip, "duration").text = str(frames)
            make_rate(a_clip, fps)
            ET.SubElement(a_clip, "in").text = "0"
            ET.SubElement(a_clip, "out").text = str(frames)
            ET.SubElement(a_clip, "start").text = str(cursor)
            ET.SubElement(a_clip, "end").text = str(cursor + frames)
            af_el = ET.SubElement(a_clip, "file", id=f"audiofile-{i+1}")
            ET.SubElement(af_el, "name").text = os.path.basename(sc.audioPath)
            ET.SubElement(af_el, "pathurl").text = path_to_url(sc.audioPath)
            make_rate(af_el, fps)
            ET.SubElement(af_el, "duration").text = str(frames)
            af_media = ET.SubElement(af_el, "media")
            af_audio = ET.SubElement(af_media, "audio")
            af_sc = ET.SubElement(af_audio, "samplecharacteristics")
            ET.SubElement(af_sc, "depth").text = "16"
            ET.SubElement(af_sc, "samplerate").text = "44100"
            ET.SubElement(af_audio, "channelcount").text = "2"

        cursor += frames

    # XML 저장
    out_path = os.path.join(out_dir, "premiere_export.xml")

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        tree.write(f, encoding="unicode")

    logger.info(f"Premiere XML 저장 완료: {out_path}")

    # ── SRT 생성 ──
    srt_path = os.path.join(out_dir, "premiere_export.srt")
    def frames_to_srt_time(f: int) -> str:
        total_ms = int(f / fps * 1000)
        ms = total_ms % 1000
        s = (total_ms // 1000) % 60
        m = (total_ms // 60000) % 60
        h = total_ms // 3600000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    srt_lines = []
    cursor_f = 0
    for i, (sc, frames) in enumerate(zip(req.scenes, scene_frames)):
        if sc.script and sc.script.strip():
            start_t = frames_to_srt_time(cursor_f)
            end_t = frames_to_srt_time(cursor_f + frames)
            srt_lines.append(f"{i+1}\n{start_t} --> {end_t}\n{sc.script.strip()}\n")
        cursor_f += frames

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    logger.info(f"SRT 저장 완료: {srt_path}")
    return {"status": "success", "xml_file": "premiere_export.xml", "srt_file": "premiere_export.srt"}


def _make_capcut_text_seg(txt: str, trange_val, position: str):
    """TextSegment 생성 with 수직 위치 적용."""
    import pycapcut as cc
    pos_map = {'bottom': -0.8, 'center': 0.0, 'top': 0.8}
    ty = pos_map.get(position, -0.8)
    clip = cc.segment.ClipSettings(transform_y=ty)
    return cc.TextSegment(txt, trange_val, clip_settings=clip)


def _apply_capcut_motion(video_seg, effect: str, total_dur_us: int):
    """CapCut VideoSegment에 움직임 키프레임 적용."""
    import pycapcut as cc
    import random as _rnd
    PAN = 0.05   # 이동량 (캔버스 폭/높이의 ~10%)
    ZOOM = 1.08  # 줌 배율

    if effect == 'random':
        effect = _rnd.choice(['zoom_in', 'zoom_out', 'pan_left', 'pan_right', 'pan_up', 'pan_down'])

    if effect == 'zoom_in':
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, 0, 1.0)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, total_dur_us, ZOOM)
    elif effect == 'zoom_out':
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, 0, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, total_dur_us, 1.0)
    elif effect == 'pan_left':
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, 0, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, total_dur_us, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_x, 0, PAN)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_x, total_dur_us, -PAN)
    elif effect == 'pan_right':
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, 0, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, total_dur_us, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_x, 0, -PAN)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_x, total_dur_us, PAN)
    elif effect == 'pan_up':
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, 0, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, total_dur_us, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_y, 0, -PAN)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_y, total_dur_us, PAN)
    elif effect == 'pan_down':
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, 0, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.uniform_scale, total_dur_us, ZOOM)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_y, 0, PAN)
        video_seg.add_keyframe(cc.keyframe.KeyframeProperty.position_y, total_dur_us, -PAN)
    # effect == 'none' → 키프레임 없음


def _apply_capcut_transition(video_seg, transition: str):
    """VideoSegment에 트랜지션 적용."""
    import pycapcut as cc
    from pycapcut.metadata.transition_meta import TransitionType
    import random as _rnd

    _MAP = {
        'dissolve':    TransitionType.叠化,
        'slide_left':  TransitionType.向左,
        'slide_right': TransitionType.向右,
        'slide_up':    TransitionType.向上,
        'slide_down':  TransitionType.向下,
        'white_flash': TransitionType.White_Flash,
        'zoom':        TransitionType.压缩,
    }
    if transition == 'random':
        transition = _rnd.choice(list(_MAP.keys()))
    t = _MAP.get(transition)
    if t:
        video_seg.add_transition(t)


def export_capcut_project(req, return_zip: bool = False) -> dict:
    """RenderRequest를 받아 CapCut 드래프트 폴더에 프로젝트를 생성.
    return_zip=True 이면 로컬 CapCut 폴더 대신 임시 폴더에 생성 후 ZIP 경로 반환."""
    try:
        import pycapcut as cc
        from pycapcut import trange
    except ImportError:
        raise RuntimeError("pyCapCut이 설치되지 않았습니다. 서버 환경에 pip install pycapcut을 실행하세요.")

    from datetime import datetime
    project_name = f"ScriptStudio_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if return_zip:
        import tempfile
        capcut_draft_path = tempfile.mkdtemp(prefix="capcut_export_")
    else:
        local_app_data = os.environ.get('LOCALAPPDATA', os.path.join(os.path.expanduser('~'), 'AppData', 'Local'))
        capcut_draft_path = os.path.join(local_app_data, 'CapCut', 'User Data', 'Projects', 'com.lveditor.draft')
        if not os.path.exists(capcut_draft_path):
            raise FileNotFoundError(
                f"CapCut 드래프트 폴더를 찾을 수 없습니다: {capcut_draft_path}\n"
                "CapCut이 설치되어 있는지 확인하고, 한 번 실행해서 드래프트 폴더를 생성해주세요."
            )

    w = req.w or 1080
    h = req.h or 1920

    draft_folder = cc.DraftFolder(capcut_draft_path)
    script = draft_folder.create_draft(project_name, w, h)

    script.add_track(cc.TrackType.video)
    script.add_track(cc.TrackType.audio)
    script.add_track(cc.TrackType.text)

    out_dir = "outputs"
    os.makedirs(out_dir, exist_ok=True)

    # return_zip 모드: 미디어를 프로젝트 폴더 내 media/ 에 저장해 ZIP에 포함
    if return_zip:
        media_dir = os.path.join(capcut_draft_path, project_name, "media")
        os.makedirs(media_dir, exist_ok=True)
    else:
        media_dir = out_dir

    def save_b64_media(b64_str: str, idx: int) -> str:
        try:
            ext = "png"
            if b64_str.startswith("data:"):
                header = b64_str.split(",", 1)[0]
                if "video/mp4" in header or "video/mpeg" in header:
                    ext = "mp4"
                elif "video/webm" in header:
                    ext = "webm"
                elif "image/jpeg" in header or "image/jpg" in header:
                    ext = "jpg"
                elif "image/webp" in header:
                    ext = "webp"
            if ',' in b64_str:
                b64_str = b64_str.split(',', 1)[1]
            img_bytes = base64.b64decode(b64_str)
            img_path = os.path.join(media_dir, f"capcut_scene_{idx+1:03d}.{ext}")
            with open(img_path, 'wb') as f:
                f.write(img_bytes)
            return img_path
        except Exception as e:
            logger.warning(f"씬 {idx+1} 미디어 저장 실패: {e}")
            return ""

    timeline_us = 0  # 마이크로초
    grok_placeholder_map = {}  # i → grok_filename (grokVideo=True but no local media)

    for i, sc in enumerate(req.scenes):
        audio_dur = sc.dur or 0
        gap_dur = sc.gapSec or 0
        total_dur = audio_dur + gap_dur
        if total_dur <= 0:
            total_dur = 3.0
            audio_dur = 3.0
        total_dur_us = int(total_dur * 1_000_000)
        audio_dur_us = int(audio_dur * 1_000_000)

        # 미디어 경로 확인
        media_path = sc.grokVideoPath or ""
        if not media_path and sc.imgDataUrl:
            if sc.imgDataUrl.startswith('data:'):
                media_path = save_b64_media(sc.imgDataUrl, i)
            elif '/download-image/' in sc.imgDataUrl:
                fname = sc.imgDataUrl.split('/download-image/')[-1].split('?')[0]
                candidate = os.path.abspath(os.path.join(out_dir, fname))
                if os.path.exists(candidate):
                    if return_zip:
                        import shutil as _sh
                        dest = os.path.join(media_dir, fname)
                        _sh.copy2(candidate, dest)
                        media_path = dest
                    else:
                        media_path = candidate

        # 미디어 없으면 검정 프레임 생성 (타임라인 연속성 유지)
        if not (media_path and os.path.exists(media_path)):
            is_grok = return_zip and bool(getattr(sc, 'grokVideo', False))
            try:
                if is_grok:
                    # Grok 비디오 플레이스홀더: MP4로 만들어야 CapCut이 video 타입으로 인식
                    black_path = os.path.join(media_dir, f"black_{i+1:03d}.mp4")
                    import subprocess as _sp_bk
                    _sp_bk.run([
                        FFMPEG_PATH, "-y", "-f", "lavfi",
                        "-i", f"color=c=black:s={w}x{h}:r=24",
                        "-t", str(max(1, int(total_dur))),
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
                        black_path
                    ], capture_output=True, timeout=30)
                    if os.path.exists(black_path):
                        media_path = black_path
                        grok_placeholder_map[i] = f"scene_{i+1:02d}_grok.mp4"
                else:
                    from PIL import Image as _PIL_Image
                    black_path = os.path.join(media_dir, f"black_{i+1:03d}.jpg")
                    _PIL_Image.new('RGB', (w, h), (0, 0, 0)).save(black_path, 'JPEG', quality=10)
                    media_path = black_path
            except Exception:
                pass

        # 비디오/이미지 클립 (gap 포함 전체 duration)
        if media_path and os.path.exists(media_path):
            video_seg = cc.VideoSegment(os.path.abspath(media_path), trange(timeline_us, total_dur_us))
            # 씬별 움직임 효과 (없으면 전역 설정 사용)
            scene_motion = getattr(sc, 'motionEffect', None) or getattr(req, 'motion_effect', 'none') or 'none'
            _apply_capcut_motion(video_seg, scene_motion, total_dur_us)
            # 씬별 트랜지션
            scene_trans = getattr(sc, 'transition', None) or 'none'
            _apply_capcut_transition(video_seg, scene_trans)
            script.add_segment(video_seg)

        # 오디오 클립 (실제 오디오 duration만 — gap 제외)
        resolved_audio = sc.audioPath or ""
        if resolved_audio.startswith('/download-audio/'):
            fname = resolved_audio.replace('/download-audio/', '').split('?')[0]
            candidate = os.path.abspath(os.path.join(out_dir, fname))
            if os.path.exists(candidate):
                if return_zip:
                    import shutil as _sh
                    dest = os.path.join(media_dir, fname)
                    _sh.copy2(candidate, dest)
                    resolved_audio = dest
                else:
                    resolved_audio = candidate
        if resolved_audio and os.path.exists(resolved_audio):
            # WAV 실제 길이로 클램프 (pymediainfo 불필요, 내장 wave 모듈 사용)
            try:
                import wave as _wave
                with _wave.open(os.path.abspath(resolved_audio), 'rb') as _wf:
                    _actual_us = int(_wf.getnframes() / _wf.getframerate() * 1_000_000)
                _safe_dur_us = min(audio_dur_us, _actual_us) if _actual_us > 0 else audio_dur_us
            except Exception:
                _safe_dur_us = audio_dur_us
            audio_seg = cc.AudioSegment(os.path.abspath(resolved_audio), trange(timeline_us, _safe_dur_us))
            script.add_segment(audio_seg)

        # 자막: subtitleTimings 있으면 청크별, 없으면 script 전체를 단일 자막으로
        # 마지막 청크는 gap까지 포함한 total_dur로 늘려서 gap 구간에 이전 자막 유지
        _remove_punct = bool(getattr(req, 'remove_punct', False))
        def _clean_sub(t):
            t = t.replace('"', '').replace("'", '').replace('-', '').replace('—', '').replace('–', '').strip()
            if _remove_punct:
                t = t.replace(',', '').replace('.', '').replace('，', '').replace('。', '')
            return t.strip()

        if sc.subtitleTimings:
            timings = [t for t in sc.subtitleTimings if (t.get('text') or '').strip()]
            for idx_t, timing in enumerate(timings):
                txt = _clean_sub(timing.get('text') or '')
                sub_start = float(timing.get('start', 0))
                sub_end = float(timing.get('end', 0))
                if idx_t == len(timings) - 1:
                    sub_end = total_dur  # 마지막 청크는 gap 끝까지
                else:
                    # 다음 자막 시작 전까지 현재 자막을 유지 (공백 구간에 앞 자막 이어지게)
                    next_start = float(timings[idx_t + 1].get('start', sub_end))
                    if next_start > sub_end:
                        sub_end = next_start
                if txt and sub_end > sub_start:
                    sub_start_us = timeline_us + int(sub_start * 1_000_000)
                    sub_dur_us = int((sub_end - sub_start) * 1_000_000)
                    if sub_dur_us > 0:
                        text_seg = _make_capcut_text_seg(txt, trange(sub_start_us, sub_dur_us), getattr(req, 'subtitle_position', 'bottom'))
                        script.add_segment(text_seg)
        elif sc.script and sc.script.strip() and total_dur_us > 0:
            chunks = build_scene_subtitle_preview(sc, req, getattr(req, 'h', 1920))
            if not chunks:
                chunks = [sc.script.strip()]
            total_chars = sum(len(c) for c in chunks) or 1
            offset_us = 0
            for ci, chunk_txt in enumerate(chunks):
                chunk_txt = _clean_sub(chunk_txt)
                proportion = len(chunk_txt) / total_chars if chunk_txt else 0
                c_dur = int(audio_dur_us * proportion)
                if ci == len(chunks) - 1:
                    c_dur = total_dur_us - offset_us  # 마지막 청크는 gap까지 포함
                if c_dur > 0 and chunk_txt:
                    text_seg = _make_capcut_text_seg(chunk_txt, trange(timeline_us + offset_us, c_dur), getattr(req, 'subtitle_position', 'bottom'))
                    script.add_segment(text_seg)
                offset_us += c_dur

        timeline_us += total_dur_us

    script.save()
    logger.info(f"CapCut 드래프트 저장 완료: {project_name}")

    if return_zip:
        import zipfile as _zf, shutil, time as _t, glob as _glob
        # JSON 파일의 임시 서버 경로를 플레이스홀더로 교체 (Companion이 로컬 경로로 복원)
        ROOT_PLACEHOLDER = "##CAPCUT_ROOT##"
        GROK_PLACEHOLDER = "##GROK_FOLDER##"
        norm_tmp = capcut_draft_path.replace("\\", "/")
        # grok 씬: black frame이 ROOT_PLACEHOLDER 교체 후 어떤 경로가 되는지 미리 계산
        norm_media = media_dir.replace("\\", "/")
        patched_media_prefix = norm_media.replace(norm_tmp, ROOT_PLACEHOLDER).replace(capcut_draft_path, ROOT_PLACEHOLDER)
        for json_file in _glob.glob(os.path.join(capcut_draft_path, "**", "*.json"), recursive=True):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    content = f.read()
                patched = content.replace(norm_tmp, ROOT_PLACEHOLDER)
                patched = patched.replace(capcut_draft_path, ROOT_PLACEHOLDER)
                # Grok 씬: black frame 경로 → ##GROK_FOLDER## 플레이스홀더로 교체
                for idx, grok_fname in grok_placeholder_map.items():
                    black_full = f"{patched_media_prefix}/black_{idx+1:03d}.mp4"
                    patched = patched.replace(black_full, f"{GROK_PLACEHOLDER}/{grok_fname}")
                if patched != content:
                    with open(json_file, "w", encoding="utf-8") as f:
                        f.write(patched)
            except Exception as e:
                logger.warning(f"JSON 경로 패치 실패 {json_file}: {e}")

        out_dir_zip = "outputs"
        os.makedirs(out_dir_zip, exist_ok=True)
        zip_name = f"capcut_export_{int(_t.time())}.zip"
        zip_path = os.path.join(out_dir_zip, zip_name)
        with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED) as zf:
            for root_d, dirs, files in os.walk(capcut_draft_path):
                for file in files:
                    fp = os.path.join(root_d, file)
                    zf.write(fp, os.path.relpath(fp, capcut_draft_path))
        shutil.rmtree(capcut_draft_path, ignore_errors=True)
        return {"status": "success", "draft_name": project_name, "zip_file": zip_name}

    return {"status": "success", "draft_name": project_name}
