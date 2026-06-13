"""FastAPI 서버만 실행 — Electron 셸에서 spawn하는 용도"""
import sys
import os
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

    data_dir = os.environ.get("SCRIPTSTUDIO_DATA_DIR", os.path.abspath("."))
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)

    os.makedirs("outputs", exist_ok=True)
    os.makedirs("outputs/temp", exist_ok=True)

    # stdout/stderr가 None이거나 broken pipe일 경우 로그 파일로 대체
    # (Electron spawn 시 콘솔 없어서 stderr 쓰기 실패 → except 블록이 터지는 버그 방지)
    log_path = os.path.join(data_dir, "server.log")
    try:
        _log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None or not sys.stdout:
            sys.stdout = _log_file
        if sys.stderr is None or not sys.stderr:
            sys.stderr = _log_file
        # 실제로 쓸 수 있는지 테스트
        sys.stderr.write("")
        sys.stderr.flush()
    except Exception:
        sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout

    # numba JIT 캐시를 writable한 경로로 설정 (frozen 앱에서 librosa 오디오 처리 정상화)
    os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(data_dir, "numba_cache"))
    os.makedirs(os.environ["NUMBA_CACHE_DIR"], exist_ok=True)

    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="error")
