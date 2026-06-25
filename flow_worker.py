"""
Google Flow 로컬 워커 v2
- ScriptStudio Railway 서버에서 Flow 작업을 폴링
- Playwright로 Google Flow 에디터 자동화
- 결과 파일을 Railway 서버에 업로드
"""

import asyncio
import os
import sys
import io
import time
import requests
from pathlib import Path
from playwright.async_api import async_playwright

# Windows cp949 콘솔에서 이모지/한글 인코딩 오류 방지
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SERVER_URL = "https://web-production-11acd.up.railway.app"
SESSION_FILE = Path(__file__).parent / "flow_session.json"
DOWNLOAD_DIR = Path(__file__).parent / "flow_output"
POLL_INTERVAL = 5
FLOW_URL = "https://labs.google/fx/ko/tools/flow"
LOCAL_MEDIA_PORT = 19284  # 로컬 미디어 서버 포트
PARALLEL_TABS = 2         # 동시 실행 탭 수


def _start_media_server():
    """flow_output/ 폴더를 HTTP로 서빙 (포트 19284)"""
    import http.server
    import threading

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            DOWNLOAD_DIR.mkdir(exist_ok=True)
            super().__init__(*args, directory=str(DOWNLOAD_DIR), **kwargs)
        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()
        def log_message(self, fmt, *args):
            pass  # 서버 로그 억제

    try:
        server = http.server.HTTPServer(("", LOCAL_MEDIA_PORT), _Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"[미디어 서버] http://localhost:{LOCAL_MEDIA_PORT}/ 시작")
    except OSError as e:
        print(f"[미디어 서버] 포트 {LOCAL_MEDIA_PORT} 이미 사용 중 (기존 서버 유지): {e}")
    except Exception as e:
        print(f"[미디어 서버] 시작 실패: {e}")


def report_done(task_id, result_url):
    try:
        requests.post(f"{SERVER_URL}/api/flow/task/{task_id}/complete",
                      json={"status": "done", "result_url": result_url}, timeout=10)
    except Exception as e:
        print(f"[완료 보고 실패] {e}")


def report_error(task_id, error):
    try:
        requests.post(f"{SERVER_URL}/api/flow/task/{task_id}/complete",
                      json={"status": "error", "error": error}, timeout=10)
        print(f"[오류 보고] {task_id}: {error}")
    except Exception as e:
        print(f"[오류 보고 실패] {e}")


def upload_file(file_path: Path) -> str:
    """로컬 미디어 서버 URL 반환 (Railway 에피메럴 스토리지 우회)"""
    return f"http://localhost:{LOCAL_MEDIA_PORT}/{file_path.name}"


async def login_and_save_session(playwright):
    browser = await playwright.chromium.launch(
        channel="chrome", headless=False,
        args=["--disable-blink-features=AutomationControlled"])
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    print("\n구글 계정 로그인 창이 열립니다. 직접 로그인해주세요.")
    await page.goto("https://accounts.google.com")
    print("로그인 완료되면 자동으로 감지합니다...")

    for _ in range(120):
        await asyncio.sleep(1)
        url = page.url
        if "accounts.google.com" not in url and "signin" not in url:
            break

    await asyncio.sleep(2)
    await context.storage_state(path=str(SESSION_FILE))
    print(f"세션 저장 완료: {SESSION_FILE}")
    await browser.close()


def _is_editor_text(text: str) -> bool:
    """에디터 내부 텍스트 패턴 확인 (에디터 사이드바에만 있는 텍스트 사용)"""
    patterns = [
        "모든 미디어",        # 에디터 사이드바
        "미디어 만들기를 시작",  # 에디터 빈 상태
        "무엇을 만들고 싶으신가요",  # 채팅 입력 placeholder
        "What would you like to make",
        "All media",         # 영문 에디터
        "휴지통",            # 에디터 사이드바
    ]
    return any(p in text for p in patterns)


async def _enter_editor(page) -> bool:
    """Flow 랜딩 → 에디터 진입. 성공하면 True"""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    await page.goto(FLOW_URL)
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(5)

    # 초기 상태 스크린샷
    shot0 = DOWNLOAD_DIR / f"debug_land_{int(time.time())}.png"
    await page.screenshot(path=str(shot0))
    body_text = await page.evaluate("() => document.body.innerText")
    print(f"[에디터] 랜딩 텍스트 앞 100자: {body_text[:100]}")

    # 이미 에디터에 있으면 바로 성공
    if _is_editor_text(body_text):
        print("[에디터] 이미 에디터 상태")
        return True

    # 1단계: 쿠키 동의 ("동의함"/"Agree")
    for sel in ["button:has-text('동의함')", "button:has-text('Agree')", "button:has-text('동의')", "button:has-text('Accept')"]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                print(f"[에디터] 쿠키 동의: {sel}")
                await asyncio.sleep(1)
                break
        except Exception:
            pass

    # 2단계: "Create with Google Flow" 클릭 → 프로젝트 목록 화면 전환
    vp = page.viewport_size or {"width": 1280, "height": 720}
    # 진단으로 확인된 버튼 위치 (483,546) + 크기 300x60 → 중심 (633, 576)
    create_x = int(vp["width"] * 0.495)  # ~633
    create_y = int(vp["height"] * 0.8)   # ~576
    print(f"[에디터] Create 버튼 클릭 ({create_x}, {create_y})")
    await page.mouse.click(create_x, create_y)
    await asyncio.sleep(4)

    shot1 = DOWNLOAD_DIR / f"debug_click_{int(time.time())}.png"
    await page.screenshot(path=str(shot1))

    # 3단계: "새 프로젝트" 버튼 클릭 (JS - 텍스트로 정확히)
    np_clicked = await page.evaluate("""() => {
        const all = document.querySelectorAll('button, [role="button"]');
        for (const el of all) {
            const t = (el.innerText || el.textContent || '').trim();
            if (t.includes('새 프로젝트') || t.includes('New project') || t.includes('add_2')) {
                el.click(); return t.slice(0, 40);
            }
        }
        return null;
    }""")
    if np_clicked:
        print(f"[에디터] 새 프로젝트 클릭: {np_clicked[:30]}")
    else:
        np_x = int(vp["width"] * 0.5)
        np_y = int(vp["height"] * 0.822)
        print(f"[에디터] 새 프로젝트 좌표 클릭 ({np_x}, {np_y})")
        await page.mouse.click(np_x, np_y)

    # 에디터 로딩 폴링 대기 (최대 25초, 클릭 없이 순수 텍스트 감지)
    print("[에디터] 에디터 로딩 대기 (최대 25초)...")
    for _ in range(25):
        await asyncio.sleep(1)
        check_text = await page.evaluate("() => document.body.innerText")
        if _is_editor_text(check_text):
            print("[에디터] 에디터 로딩 완료")
            shot2 = DOWNLOAD_DIR / f"debug_final_{int(time.time())}.png"
            await page.screenshot(path=str(shot2))
            return True

    # 최종 확인
    final_text = await page.evaluate("() => document.body.innerText")
    in_editor = _is_editor_text(final_text)

    # 실패 시 스크린샷 저장
    shot2 = DOWNLOAD_DIR / f"debug_final_{int(time.time())}.png"
    await page.screenshot(path=str(shot2))
    print(f"[에디터] 진입 {'성공' if in_editor else '실패'} | 텍스트: {final_text[:80]}")
    return in_editor


async def _find_chat_input(page):
    """Flow 채팅 입력창 찾기 - 여러 셀렉터 시도"""
    selectors = [
        "textarea",
        "[role='textbox']",
        "[contenteditable='true']",
        "[data-testid*='input']",
        "[placeholder*='만들']",
        "[placeholder*='What']",
        "[placeholder*='create']",
        "input[type='text']",
        ".chat-input",
        "[class*='input']",
        "[class*='prompt']",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1500):
                print(f"[전송] 입력창 발견: {sel}")
                return el
        except Exception:
            pass
    return None


async def _send_prompt(page, prompt: str, image_path: str = "") -> bool:
    """프롬프트 (+ 이미지) 전송. 성공하면 True"""
    # 입력창 찾기 전 스크린샷
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    await page.screenshot(path=str(DOWNLOAD_DIR / f"debug_before_input_{int(time.time())}.png"))

    # 이미지 업로드 시도
    if image_path and os.path.exists(image_path):
        uploaded = False

        # 방법 1: 드롭 이벤트 (중앙 "미디어를 드롭하세요" 영역)
        try:
            import base64 as _b64i
            with open(image_path, 'rb') as _fi:
                _b64data = _b64i.b64encode(_fi.read()).decode()
            drop_result = await page.evaluate("""async (b64data) => {
                const byteStr = atob(b64data);
                const ab = new ArrayBuffer(byteStr.length);
                const ia = new Uint8Array(ab);
                for (let i = 0; i < byteStr.length; i++) ia[i] = byteStr.charCodeAt(i);
                const blob = new Blob([ab], {type: 'image/png'});
                const file = new File([blob], 'upload.png', {type: 'image/png'});
                const dt = new DataTransfer();
                dt.items.add(file);
                // 채팅창 또는 중앙 드롭 영역 순서로 시도
                const selectors = [
                    'textarea', '[role="textbox"]', '[contenteditable="true"]',
                    'main', '.main', '[class*="canvas"]', '[class*="editor-area"]', 'body'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    ['dragenter','dragover','drop'].forEach(ev => {
                        el.dispatchEvent(new DragEvent(ev, {bubbles:true, cancelable:true, dataTransfer:dt}));
                    });
                    return sel;
                }
                return null;
            }""", _b64data)
            if drop_result:
                print(f"[전송] 이미지 드롭 이벤트: {drop_result}")
                await asyncio.sleep(3)
                uploaded = True
        except Exception as e1:
            print(f"[전송] 드롭 이벤트 실패: {e1}")

        if not uploaded:
            # 방법 2: + 버튼 좌표 클릭 (뷰포트 하단 좌측) → 파일 선택창
            try:
                vp = page.viewport_size or {"width": 1280, "height": 720}
                # 스크린샷에서 + 버튼 위치: 채팅 하단 좌측 (약 990, 665)
                plus_x = int(vp["width"] * 0.773)
                plus_y = int(vp["height"] * 0.923)
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await page.mouse.click(plus_x, plus_y)
                fc = await fc_info.value
                await fc.set_files(image_path)
                print(f"[전송] 이미지 업로드 (+버튼 좌표 {plus_x},{plus_y}): {image_path}")
                await asyncio.sleep(3)
                uploaded = True
            except Exception as e2:
                print(f"[전송] + 버튼 좌표 클릭 실패: {e2}")

        if not uploaded:
            # 방법 3: file input 직접 (React 앱에서 인식 안 될 수 있음, 최후 수단)
            try:
                file_inputs = page.locator("input[type='file']")
                if await file_inputs.count() > 0:
                    await file_inputs.first.set_input_files(image_path)
                    print(f"[전송] 이미지 업로드 (file input 직접): {image_path}")
                    await asyncio.sleep(3)
                    uploaded = True
            except Exception as e3:
                print(f"[전송] file input 직접 실패: {e3}")

        if not uploaded:
            print(f"[전송] 이미지 업로드 모두 실패 — 텍스트 프롬프트만 전송")

    # 프롬프트 입력
    box = await _find_chat_input(page)
    if box is None:
        print("[전송] 입력창을 찾을 수 없음 - JS로 클릭 시도")
        # JS로 입력창 강제 클릭
        result = await page.evaluate("""(prompt) => {
            const inputs = document.querySelectorAll(
                'textarea, [role="textbox"], [contenteditable="true"], input[type="text"]'
            );
            for (const el of inputs) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 100 && rect.height > 10) {
                    el.focus();
                    el.click();
                    return 'found:' + el.tagName + ':' + (el.getAttribute('role') || '') + ':' + rect.width + 'x' + rect.height;
                }
            }
            return null;
        }""", prompt)
        print(f"[전송] JS 입력창 탐색 결과: {result}")
        if not result:
            return False
        await page.keyboard.type(prompt)
    else:
        try:
            await box.click()
            await asyncio.sleep(0.5)
            await box.fill(prompt)
        except Exception:
            await box.click()
            await page.keyboard.type(prompt)
        print(f"[전송] 프롬프트 입력: {prompt[:60]}")

    await asyncio.sleep(1)
    # 전송 (엔터키)
    await page.keyboard.press("Enter")
    print("[전송] 엔터 입력")
    await asyncio.sleep(2)
    return True


async def _wait_and_screenshot(page, label: str, wait_sec: int = 0) -> str:
    """대기 후 스크린샷 저장, 경로 반환"""
    if wait_sec:
        await asyncio.sleep(wait_sec)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    path = DOWNLOAD_DIR / f"debug_{label}_{int(time.time())}.png"
    await page.screenshot(path=str(path), full_page=False)
    print(f"[스크린샷] {path}")
    return str(path)


async def _try_download(page, result_type: str) -> str:
    """우클릭 메뉴 또는 다운로드 버튼으로 결과 저장"""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    ext = "mp4" if result_type == "video" else "png"
    save_path = DOWNLOAD_DIR / f"flow_{result_type}_{int(time.time())}.{ext}"

    # 방법 1: 다운로드 버튼 찾기
    for sel in [
        "button:has-text('다운로드')", "button:has-text('Download')",
        "[aria-label*='다운로드']", "[aria-label*='download']",
        "button:has-text('내보내기')", "button:has-text('Export')"
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                async with page.expect_download(timeout=20000) as dl_info:
                    await btn.click()
                dl = await dl_info.value
                await dl.save_as(str(save_path))
                print(f"[다운로드] 저장: {save_path}")
                return str(save_path)
        except Exception:
            pass

    # 방법 2: 생성된 미디어 우클릭 → 저장
    try:
        # 모든 미디어 탭의 첫 번째 아이템 클릭
        media_item = page.locator("[role='listitem'] img, [role='listitem'] video, .media-item, [data-media]").first
        if await media_item.is_visible(timeout=3000):
            await media_item.click(button="right")
            await asyncio.sleep(1)
            save_btn = page.locator("text=다운로드, text=Save, text=Download").first
            async with page.expect_download(timeout=15000) as dl_info:
                await save_btn.click()
            dl = await dl_info.value
            await dl.save_as(str(save_path))
            print(f"[다운로드] 우클릭 저장: {save_path}")
            return str(save_path)
    except Exception as e:
        print(f"[다운로드] 우클릭 실패: {e}")

    return ""


async def _count_media_items(page) -> int:
    """생성된 미디어(이미지+비디오) 수 반환 (80px+, blob/http URL)"""
    try:
        count = await page.evaluate("""() => {
            let n = 0;
            for (const img of document.querySelectorAll('img')) {
                const src = img.src || img.currentSrc || '';
                const rect = img.getBoundingClientRect();
                if (src && (src.startsWith('http') || src.startsWith('blob:')) &&
                    rect.width >= 80 && rect.height >= 80 &&
                    !src.includes('favicon') && !src.includes('logo') &&
                    !src.includes('avatar') && !src.includes('profile') && !src.includes('icon')) {
                    n++;
                }
            }
            for (const vid of document.querySelectorAll('video')) {
                const rect = vid.getBoundingClientRect();
                if (rect.width >= 80 && rect.height >= 80) n++;
            }
            return n;
        }""")
        return count
    except Exception:
        return 0


async def _download_video_element(page) -> str:
    """video 요소 캡처 또는 다운로드 버튼으로 영상 저장"""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    save_path = DOWNLOAD_DIR / f"flow_video_{int(time.time())}.mp4"

    # 방법 1: 다운로드 버튼 클릭
    for sel in ["button:has-text('다운로드')", "button:has-text('Download')",
                "[aria-label*='다운로드']", "[aria-label*='download']",
                "[aria-label*='Download']", "button:has-text('저장')"]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                async with page.expect_download(timeout=60000) as dl_info:
                    await btn.click()
                dl = await dl_info.value
                await dl.save_as(str(save_path))
                print(f"[다운로드] 영상 다운로드 버튼 성공: {save_path}")
                return str(save_path)
        except Exception:
            pass

    # 방법 2: video 요소 hover → 다운로드 버튼
    try:
        vid = page.locator("video").first
        if await vid.is_visible(timeout=3000):
            await vid.hover()
            await asyncio.sleep(1)
            for sel in ["button:has-text('다운로드')", "[aria-label*='download']", "[aria-label*='Download']"]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1500):
                        async with page.expect_download(timeout=60000) as dl_info:
                            await btn.click()
                        dl = await dl_info.value
                        await dl.save_as(str(save_path))
                        print(f"[다운로드] hover→다운로드 성공: {save_path}")
                        return str(save_path)
                except Exception:
                    pass
    except Exception:
        pass

    # 방법 3: video src blob → base64 → MP4 저장
    try:
        result = await page.evaluate("""async () => {
            const vid = document.querySelector('video');
            if (!vid) return {err: 'no video element'};
            const src = vid.src || vid.currentSrc || '';
            if (!src) return {err: 'no src', paused: vid.paused, readyState: vid.readyState};
            try {
                const resp = await fetch(src);
                const buf = await resp.arrayBuffer();
                const arr = new Uint8Array(buf);
                let s = '';
                const chunk = 8192;
                for (let i = 0; i < arr.length; i += chunk) {
                    s += String.fromCharCode(...arr.subarray(i, Math.min(i+chunk, arr.length)));
                }
                return {src: src.slice(0,60), size: arr.length, b64: btoa(s)};
            } catch(e) { return {src: src.slice(0,60), err: String(e)}; }
        }""")
        print(f"[다운로드] blob 결과: {str(result)[:200]}")
        if result and result.get("b64"):
            import base64 as _b64
            import subprocess as _sp
            data = _b64.b64decode(result["b64"])
            raw_path = DOWNLOAD_DIR / f"flow_video_raw_{int(time.time())}.mp4"
            mp4_path = DOWNLOAD_DIR / f"flow_video_{int(time.time())}.mp4"
            raw_path.write_bytes(data)
            # moov atom을 파일 앞으로 이동 (웹 스트리밍 호환)
            try:
                _sp.run(
                    ["ffmpeg", "-y", "-i", str(raw_path),
                     "-c", "copy", "-movflags", "faststart", str(mp4_path)],
                    capture_output=True, timeout=60
                )
                raw_path.unlink(missing_ok=True)
                print(f"[다운로드] faststart MP4 저장: {mp4_path} ({mp4_path.stat().st_size} bytes)")
            except Exception as fe:
                print(f"[다운로드] ffmpeg 실패({fe}), 원본 사용")
                raw_path.rename(mp4_path)
            return str(mp4_path)
    except Exception as e:
        print(f"[다운로드] blob 변환 오류: {e}")

    # 방법 4: video 요소 스크린샷 (최종 폴백 — 프레임만)
    try:
        vid_loc = page.locator("video").first
        if await vid_loc.is_visible(timeout=2000):
            frame_path = DOWNLOAD_DIR / f"flow_video_frame_{int(time.time())}.png"
            await vid_loc.screenshot(path=str(frame_path))
            print(f"[다운로드] 영상 프레임 캡처(폴백): {frame_path}")
            return str(frame_path)
    except Exception as e:
        print(f"[다운로드] 영상 요소 캡처 실패: {e}")
    return ""


async def _download_image_via_browser(page, result_type: str) -> str:
    """생성된 이미지 요소를 Playwright locator.screenshot()으로 직접 캡처 (CORS 우회)"""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    save_path = DOWNLOAD_DIR / f"flow_{result_type}_{int(time.time())}.png"
    try:
        imgs = page.locator("img")
        count = await imgs.count()
        best = None
        best_area = 0
        best_src = ""
        for i in range(count):
            img = imgs.nth(i)
            src = await img.get_attribute("src") or ""
            if any(x in src for x in ["favicon", "logo", "avatar", "profile", "icon"]):
                continue
            box = await img.bounding_box()
            if box and box["width"] >= 80 and box["height"] >= 80:
                area = box["width"] * box["height"]
                if area > best_area:
                    best_area = area
                    best = img
                    best_src = src
        if best:
            # src URL로 직접 다운로드 (캡션 오버레이 없는 원본)
            if best_src:
                try:
                    import base64
                    b64 = await page.evaluate("""async (src) => {
                        const r = await fetch(src);
                        const blob = await r.blob();
                        return await new Promise(res => {
                            const fr = new FileReader();
                            fr.onload = () => res(fr.result.split(',')[1]);
                            fr.readAsDataURL(blob);
                        });
                    }""", best_src)
                    if b64:
                        save_path.write_bytes(base64.b64decode(b64))
                        sz = save_path.stat().st_size
                        print(f"[다운로드] 이미지 URL 직접 다운로드 성공: {save_path} ({sz}bytes)")
                        return str(save_path)
                except Exception as fe:
                    print(f"[다운로드] URL 다운로드 실패 ({fe}), 스크린샷 방식으로 대체")
            # fallback: 스크린샷 (캡션 포함될 수 있음)
            await best.screenshot(path=str(save_path))
            sz = save_path.stat().st_size
            print(f"[다운로드] 이미지 요소 캡처(스크린샷): {save_path} ({sz}bytes, area={best_area:.0f})")
            return str(save_path)
        else:
            print("[다운로드] 80px+ 이미지 요소 없음")
    except Exception as e:
        print(f"[다운로드] 이미지 요소 캡처 실패: {e}")
    return ""


async def _hover_and_download(page, result_type: str) -> str:
    """미디어 아이템에 호버 → 다운로드 버튼 클릭"""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    ext = "mp4" if result_type == "video" else "png"
    save_path = DOWNLOAD_DIR / f"flow_{result_type}_{int(time.time())}.{ext}"

    # 모든 미디어 아이템 (최신 = 첫번째 또는 마지막)
    for locator_str in [
        "[role='listitem'] img",
        "[role='listitem'] video",
        "[class*='media'] img",
        "[class*='thumbnail'] img"
    ]:
        try:
            items = page.locator(locator_str)
            count = await items.count()
            if count == 0:
                continue
            # 마지막 아이템 (가장 최근)
            item = items.nth(count - 1)
            await item.hover()
            await asyncio.sleep(1)
            # 다운로드 버튼 찾기
            for dl_sel in [
                "button:has-text('다운로드')", "button:has-text('Download')",
                "[aria-label*='다운로드']", "[aria-label*='download']",
                "[title*='다운로드']", "[title*='Download']"
            ]:
                try:
                    dl_btn = page.locator(dl_sel).first
                    if await dl_btn.is_visible(timeout=1500):
                        async with page.expect_download(timeout=20000) as dl_info:
                            await dl_btn.click()
                        dl = await dl_info.value
                        await dl.save_as(str(save_path))
                        print(f"[다운로드] 성공: {save_path}")
                        return str(save_path)
                except Exception:
                    pass
        except Exception:
            pass

    return ""


async def _run_single_generation(page, prompt: str, local_image: str, mode: str,
                                 result_screenshot_label: str = "result") -> tuple:
    """에디터에서 한 번 생성 실행. (save_path, result_screenshot) 반환"""
    before_count = await _count_media_items(page)
    print(f"[생성] 전 미디어 수: {before_count}")
    await _wait_and_screenshot(page, "editor")

    # 네트워크 인터셉트로 영상 URL 캡처
    captured_video_path = []
    if mode in ("img2video", "text2img_video"):
        async def _on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                url = response.url
                if ("video" in ct or url.endswith(".mp4") or url.endswith(".webm")) and \
                        "favicon" not in url:
                    size_hint = response.headers.get("content-length", "?")
                    print(f"[네트워크] 영상 응답: {url[:80]} | {ct} | {size_hint}bytes")
                    body = await response.body()
                    if len(body) > 100_000:  # 100KB 이상만
                        ts = int(time.time())
                        vpath = DOWNLOAD_DIR / f"flow_net_video_{ts}.mp4"
                        vpath.write_bytes(body)
                        print(f"[네트워크] 영상 저장: {vpath} ({len(body)} bytes)")
                        captured_video_path.append(str(vpath))
            except Exception:
                pass
        page.on("response", _on_response)

    sent = await _send_prompt(page, prompt, local_image)
    if not sent:
        raise RuntimeError("프롬프트 전송 실패")

    max_wait = 36 if mode == "img2video" else 30  # 영상은 최대 6분
    print(f"[생성] 최대 {max_wait * 10 // 60}분 대기...")
    result_screenshot = ""
    popup_clicked = False
    for i in range(max_wait):
        await asyncio.sleep(10)

        # AI 팝업 자동 응답 (크레딧 승인 / 재생시간 선택 등)
        if mode in ("img2video", "text2img_video"):
            clicked = await page.evaluate("""() => {
                // 승인 계열 — "확인" 단독 제거 (너무 일반적), "승인" 등 정확한 텍스트만
                const approveTargets = ['승인, 다시 묻지 않음', "Don't ask again",
                                        '승인', 'Approve', 'Continue'];
                // 재생시간 선택 (5초 불가 시 8초 선택)
                const durationTargets = ['8 seconds', '8초', '6 seconds', '6초'];

                const allTargets = [...approveTargets, ...durationTargets];
                // div/span 포함 (Flow 팝업 버튼이 div 기반)
                const selectors = 'button, [role="button"], [role="option"], li, [tabindex], div, span, label';
                for (const el of document.querySelectorAll(selectors)) {
                    const t = (el.innerText || el.textContent || '').trim();
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) continue;
                    for (const target of allTargets) {
                        if (t === target || t.endsWith(target)) {
                            el.click(); return t.slice(0,30);
                        }
                    }
                }
                return null;
            }""")
            if clicked:
                print(f"[생성] AI 팝업 자동 클릭: {clicked}")
                popup_clicked = True
                await asyncio.sleep(3)

        after_count = await _count_media_items(page)
        elapsed = (i + 1) * 10
        print(f"[생성] {elapsed}초 | 미디어 수: {before_count} → {after_count}")
        if after_count > before_count:
            print(f"[생성] 새 미디어 감지! ({after_count - before_count}개 추가)")
            await asyncio.sleep(3)
            result_screenshot = await _wait_and_screenshot(page, result_screenshot_label)
            break
    else:
        result_screenshot = await _wait_and_screenshot(page, "timeout")

    is_video = mode in ("img2video", "text2img_video")
    save_path = ""

    # 네트워크 인터셉트로 캡처된 영상 우선 사용
    if is_video and captured_video_path:
        raw = captured_video_path[-1]  # 가장 최근 것
        import subprocess as _sp2
        fast_path = str(DOWNLOAD_DIR / f"flow_video_{int(time.time())}.mp4")
        try:
            _sp2.run(["ffmpeg", "-y", "-i", raw, "-c", "copy",
                      "-movflags", "faststart", fast_path],
                     capture_output=True, timeout=60)
            Path(raw).unlink(missing_ok=True)
            save_path = fast_path
            print(f"[다운로드] 네트워크 캡처 faststart 완료: {save_path}")
        except Exception:
            save_path = raw
        page.remove_listener("response", _on_response)

    if is_video and not save_path:
        save_path = await _download_video_element(page)

    if not save_path:
        save_path = await _download_image_via_browser(page, "video" if is_video else "image")

    if not save_path:
        save_path = await _hover_and_download(page, "video" if is_video else "image")

    if not save_path:
        save_path = await _try_download(page, "video" if is_video else "image")

    if not save_path and result_screenshot:
        save_path = result_screenshot
        print(f"[다운로드] 폴백 - 스크린샷 사용: {save_path}")

    return save_path, result_screenshot


def _has_korean(text: str) -> bool:
    return any('가' <= c <= '힣' for c in text)


async def run_task(page, task: dict) -> str:
    """단일 작업 실행. 결과 파일 경로 반환 (실패시 "")"""
    mode = task["mode"]
    prompt = task.get("prompt", "")
    delay = int(task.get("delay", 5))
    image_url = task.get("image_url", "")


    in_editor = await _enter_editor(page)
    if not in_editor:
        raise RuntimeError("에디터 진입 실패")

    if mode == "text2img_video":
        # Step 1: 이미지 생성
        print("[text2img_video] 1단계: 이미지 생성 중...")
        img_prompt = prompt or "cinematic scene"
        img_path, _ = await _run_single_generation(page, img_prompt, "", "text2img", "img_result")
        if not img_path or not os.path.exists(img_path):
            raise RuntimeError("이미지 생성 실패")

        # Step 2: 에디터 재진입 후 영상 생성
        print("[text2img_video] 2단계: 영상 생성 중...")
        in_editor2 = await _enter_editor(page)
        if not in_editor2:
            raise RuntimeError("에디터 재진입 실패 (영상 생성용)")

        video_prompt = f"Create a smooth cinematic video: {prompt}"
        video_path, _ = await _run_single_generation(page, video_prompt, img_path, "img2video", "video_result")

        if delay > 0:
            await asyncio.sleep(delay)
        return video_path or img_path  # 영상 실패 시 이미지라도 반환

    # 기본 text2img / img2video
    local_image = ""
    if image_url:
        try:
            DOWNLOAD_DIR.mkdir(exist_ok=True)
            local_image = str(DOWNLOAD_DIR / f"src_{int(time.time())}.png")
            r = requests.get(image_url, timeout=30)
            Path(local_image).write_bytes(r.content)
        except Exception as e:
            print(f"[이미지 다운로드 실패] {e}")
            local_image = ""

    # 프롬프트에서 초수 지정 제거 ([5 seconds], [10 seconds] 등 → Flow AI가 지원 안 하면 팝업 뜸)
    import re as _re
    prompt = _re.sub(r'\[\s*\d+\s*seconds?\s*\]', '', prompt, flags=_re.IGNORECASE).strip()

    # img2video: 피사체 포함한 완전한 영상 프롬프트 구성
    if mode == "img2video" and prompt and not prompt.lower().startswith("create"):
        prompt = f"Create a smooth cinematic video: {prompt}"
        print(f"[생성] img2video 프롬프트 보완: {prompt[:80]}")

    save_path, _ = await _run_single_generation(page, prompt, local_image, mode)

    if delay > 0:
        await asyncio.sleep(delay)
    return save_path


async def run_worker():
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    _start_media_server()
    print(f"Flow 워커 v2 시작 (병렬 {PARALLEL_TABS}탭) | 서버: {SERVER_URL}")

    async def _create_browser_ctx(p):
        b = await p.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await b.new_context(
            storage_state=str(SESSION_FILE),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
        return b, ctx

    async with async_playwright() as p:
        if not SESSION_FILE.exists():
            print("세션 없음 → 웹에서 '구글 로그인' 버튼을 눌러주세요 (자동 실행 안 함)")
            while not SESSION_FILE.exists():
                try:
                    resp = requests.get(f"{SERVER_URL}/api/flow/task/poll", timeout=10)
                    task = resp.json()
                    if task.get("login_requested"):
                        print("로그인 요청 수신 → 구글 로그인 창 열기")
                        await login_and_save_session(p)
                        break
                except Exception as pe:
                    print(f"poll 오류: {pe}")
                await asyncio.sleep(POLL_INTERVAL)

        # 시작 시 브라우저를 열지 않음 — 실제 작업이나 로그인 요청 시에만 열기
        browser = None
        ctx = None
        poll_lock = asyncio.Lock()
        browser_lock = asyncio.Lock()
        print(f"워커 대기 중 (브라우저는 첫 작업 시 열림)...\n")

        async def worker_tab(tab_id: int):
            nonlocal browser, ctx
            page = None
            print(f"[탭 {tab_id}] 시작")

            while True:
                task = None
                try:
                    # 페이지 유효성 확인 (page가 있을 때만)
                    if page is not None:
                        try:
                            await page.evaluate("1")
                        except Exception:
                            print(f"[탭 {tab_id}] 페이지 닫힘 → 새 탭 생성")
                            try:
                                await page.close()
                            except Exception:
                                pass
                            try:
                                page = await ctx.new_page()
                            except Exception:
                                page = None
                            await asyncio.sleep(2)

                    # poll_lock으로 동시 poll 방지 (동일 태스크 중복 할당 차단)
                    async with poll_lock:
                        try:
                            resp = requests.get(f"{SERVER_URL}/api/flow/task/poll", timeout=10)
                            task = resp.json()
                        except Exception as pe:
                            print(f"[탭 {tab_id}] poll 오류: {pe}")
                            await asyncio.sleep(POLL_INTERVAL)
                            continue

                    if task.get("login_requested"):
                        print(f"[탭 {tab_id}] 로그인 요청 → 새 구글 로그인 창 열기")
                        async with browser_lock:
                            if browser:
                                try:
                                    await browser.close()
                                except Exception:
                                    pass
                                browser = None
                                ctx = None
                        page = None
                        if SESSION_FILE.exists():
                            SESSION_FILE.unlink()
                        await login_and_save_session(p)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    if not task.get("task_id"):
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # task 있을 때만 브라우저/탭 생성
                    async with browser_lock:
                        if browser is None:
                            if not SESSION_FILE.exists():
                                print(f"[탭 {tab_id}] 세션 없음 — 구글 로그인 필요")
                                report_error(task["task_id"], "구글 로그인 필요 — 웹에서 '구글 로그인' 버튼을 눌러주세요")
                                await asyncio.sleep(POLL_INTERVAL)
                                continue
                            print("첫 작업 감지 → 브라우저 시작")
                            browser, ctx = await _create_browser_ctx(p)

                    if page is None:
                        try:
                            page = await ctx.new_page()
                        except Exception as ne:
                            print(f"[탭 {tab_id}] 탭 생성 실패: {ne}")
                            await asyncio.sleep(POLL_INTERVAL)
                            continue

                    task_id = task["task_id"]
                    print(f"\n[탭 {tab_id}] 작업: {task_id} | mode={task['mode']}")

                    save_path = await run_task(page, task)

                    if save_path and "debug_timeout" in str(save_path):
                        report_error(task_id, "Flow 타임아웃 — 생성 실패 (프롬프트가 너무 길거나 한국어일 수 있음)")
                        print(f"[탭 {tab_id}] 실패: debug_timeout")
                    elif save_path and os.path.exists(save_path):
                        result_url = upload_file(Path(save_path))
                        report_done(task_id, result_url or f"file://{save_path}")
                        print(f"[탭 {tab_id}] 완료 → {result_url or save_path}")
                    else:
                        report_error(task_id, "다운로드 실패 — 결과 파일 없음")

                except KeyboardInterrupt:
                    print(f"\n[탭 {tab_id}] 종료")
                    break
                except Exception as e:
                    err_str = str(e).lower()
                    print(f"[탭 {tab_id}] 오류: {e}")
                    if task and task.get("task_id"):
                        report_error(task["task_id"], str(e))
                    if any(k in err_str for k in ["closed", "disconnected", "target page", "browser has been"]):
                        print(f"[탭 {tab_id}] 브라우저 종료 → 탭 재생성")
                        try:
                            await page.close()
                        except Exception:
                            pass
                        try:
                            page = await ctx.new_page()
                        except Exception as ne:
                            print(f"[탭 {tab_id}] 탭 재생성 실패: {ne}")
                            page = None
                    await asyncio.sleep(POLL_INTERVAL)

            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

        try:
            await asyncio.gather(*[worker_tab(i) for i in range(PARALLEL_TABS)])
        except KeyboardInterrupt:
            print("\n워커 종료")
        finally:
            if browser:
                await browser.close()


if __name__ == "__main__":
    log_file = Path(__file__).parent / "flow_worker.log"
    if "--bg" in sys.argv:
        sys.stdout = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    asyncio.run(run_worker())
