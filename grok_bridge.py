"""
MakeLens Grok Bridge
메이크렌즈와 Chrome 확장프로그램 간의 WebSocket 통신 브릿지
"""

import asyncio
import json
import base64
import os
from typing import Optional, Callable, Dict, Any
from io import BytesIO

try:
    import websockets
    from websockets.server import serve
except ImportError:
    print("websockets 패키지가 필요합니다: pip install websockets")
    raise


class GrokBridge:
    """메이크렌즈와 Grok 확장프로그램 간의 통신 브릿지"""

    def __init__(self, port: int = 9876):
        self.port = port
        self.server = None
        self.clients = set()
        self.is_running = False

        # 콜백 함수들
        self.on_connected: Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None
        self.on_task_completed: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_task_failed: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_progress_update: Optional[Callable[[int], None]] = None
        self.on_queue_status: Optional[Callable[[Dict[str, Any]], None]] = None  # 큐 상태 콜백
        self.on_video_moved: Optional[Callable[[Dict[str, Any]], None]] = None  # 비디오 파일 이동 완료 콜백
        self.on_task_removed: Optional[Callable[[int], None]] = None  # 확장앱에서 작업 삭제 콜백
        self.on_all_tasks_removed: Optional[Callable[[], None]] = None  # 확장앱에서 전체 취소 콜백
        self.on_retry_scene_request: Optional[Callable[[int], None]] = None  # 확장앱에서 장면 재실행 요청 콜백

        # 작업 관리
        self.pending_tasks: Dict[str, asyncio.Future] = {}
        self.task_queue = asyncio.Queue()

        # [Fix] 작업 생성 시점의 저장 경로를 기억 (프로젝트 전환 시 엉뚱한 폴더 저장 방지)
        # key: scene_no(int), value: download_folder(str) - 작업 생성 당시의 경로
        self._task_target_folders: Dict[int, str] = {}

        # 프로젝트 경로 정보
        self.project_path: str = ''  # 현재 프로젝트 전체 경로
        self.download_folder: str = ''  # 비디오 다운로드 폴더 경로
        self.queue_paused: bool = False  # 확장앱 대기열 일시정지 상태
        self.queue_count: int = 0
        self.is_processing: bool = False
        self.current_scene_no = None
        self.completed_count: int = 0

    async def start(self):
        """WebSocket 서버 시작"""
        if self.is_running:
            return

        try:
            self.server = await serve(
                self._handle_connection,
                "localhost",
                self.port,
                max_size=50 * 1024 * 1024  # 50MB (이미지 Base64 전송용)
            )
            self.is_running = True
            print(f"[GrokBridge] WebSocket 서버 시작: ws://localhost:{self.port}")

        except OSError as e:
            # 포트가 이미 사용 중인 경우, 기존 프로세스 종료 후 재시도
            if e.errno in (10048, 98):  # 10048=Windows WSAEADDRINUSE, 98=Linux EADDRINUSE
                print(f"[GrokBridge] 포트 {self.port} 사용 중 → 기존 프로세스 종료 후 재시도")
                self._kill_port_process(self.port)
                await asyncio.sleep(0.5)
                try:
                    self.server = await serve(
                        self._handle_connection,
                        "localhost",
                        self.port,
                        max_size=50 * 1024 * 1024
                    )
                    self.is_running = True
                    print(f"[GrokBridge] WebSocket 서버 시작 (재시도 성공): ws://localhost:{self.port}")
                except Exception as e2:
                    print(f"[GrokBridge] 서버 시작 실패 (재시도): {e2}")
                    raise
            else:
                print(f"[GrokBridge] 서버 시작 실패: {e}")
                raise
        except Exception as e:
            print(f"[GrokBridge] 서버 시작 실패: {e}")
            raise

    @staticmethod
    def _kill_port_process(port: int):
        """지정 포트를 점유하는 프로세스 종료 (Windows/Linux)"""
        import subprocess, sys
        try:
            if sys.platform == 'win32':
                result = subprocess.run(
                    f'netstat -ano | findstr :{port} | findstr LISTENING',
                    shell=True, capture_output=True, text=True
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if parts:
                        pid = parts[-1]
                        subprocess.run(f'taskkill /F /PID {pid}', shell=True,
                                       capture_output=True)
                        print(f"[GrokBridge] PID {pid} 종료")
            else:
                subprocess.run(f'fuser -k {port}/tcp', shell=True,
                               capture_output=True)
        except Exception as ex:
            print(f"[GrokBridge] 포트 정리 실패: {ex}")

    async def stop(self):
        """WebSocket 서버 중지"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.is_running = False
            print("[GrokBridge] 서버 중지됨")

    async def _handle_connection(self, websocket):
        """클라이언트 연결 처리"""
        self.clients.add(websocket)
        print(f"[GrokBridge] 클라이언트 연결됨 (총 {len(self.clients)}개)")

        if self.on_connected:
            self.on_connected()

        try:
            async for message in websocket:
                await self._handle_message(websocket, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            print(f"[GrokBridge] 클라이언트 연결 해제 (총 {len(self.clients)}개)")

            if self.on_disconnected:
                self.on_disconnected()

    async def _handle_message(self, websocket, message):
        """메시지 처리"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')

            if msg_type != 'PING':
                print(f"[GrokBridge] 메시지 수신: {msg_type}")

            if msg_type == 'EXTENSION_READY':
                # 확장프로그램 준비 완료
                print("[GrokBridge] 확장프로그램 준비 완료")

            elif msg_type == 'TASK_COMPLETED':
                # 작업 완료
                task_id = data.get('taskId')
                if task_id in self.pending_tasks:
                    self.pending_tasks[task_id].set_result(data)
                    del self.pending_tasks[task_id]

                if self.on_task_completed:
                    self.on_task_completed(data)

            elif msg_type == 'TASK_FAILED':
                # 작업 실패
                task_id = data.get('taskId')
                error_code = data.get('errorCode', 'UNKNOWN')

                # [Fix] 실패한 작업의 저장 경로 기록 정리
                failed_scene = data.get('sceneNo')
                if failed_scene and failed_scene in self._task_target_folders:
                    del self._task_target_folders[failed_scene]

                if task_id in self.pending_tasks:
                    self.pending_tasks[task_id].set_exception(
                        Exception(data.get('error', 'Unknown error'))
                    )
                    del self.pending_tasks[task_id]

                if self.on_task_failed:
                    # errorCode 포함하여 전달
                    self.on_task_failed(data)

            elif msg_type == 'PROGRESS_UPDATE':
                # 진행률 업데이트 (sceneNo 포함)
                if self.on_progress_update:
                    self.on_progress_update(data.get('progress', 0), data.get('sceneNo'))

            elif msg_type == 'STATUS':
                # 상태 응답
                print(f"[GrokBridge] 상태: 대기열 {data.get('queue', 0)}, 완료 {data.get('completed', 0)}")

            elif msg_type == 'QUEUE_STATUS':
                # 큐 상태 업데이트 (확장앱에서 주기적으로 전송)
                self.queue_count = data.get('queueCount', 0)
                self.is_processing = data.get('isProcessing', False)
                self.current_scene_no = data.get('currentSceneNo')
                self.completed_count = data.get('completedCount', 0)
                self.queue_paused = data.get('queuePaused', False)

                print(f"[GrokBridge] 큐 상태: 대기 {self.queue_count}, 진행중 {self.is_processing}, 현재씬 {self.current_scene_no}, 완료 {self.completed_count}, 일시정지 {self.queue_paused}")

                if self.on_queue_status:
                    self.on_queue_status({
                        'queueCount': self.queue_count,
                        'isProcessing': self.is_processing,
                        'currentSceneNo': self.current_scene_no,
                        'completedCount': self.completed_count,
                        'queuePaused': self.queue_paused
                    })

            elif msg_type == 'TASK_REMOVED':
                # 확장앱에서 개별 작업 삭제
                scene_no = data.get('sceneNo')
                print(f"[GrokBridge] 작업 삭제 알림: 장면 {scene_no}")
                # [Fix] 삭제된 작업의 저장 경로 기록 정리
                if scene_no and scene_no in self._task_target_folders:
                    del self._task_target_folders[scene_no]
                if self.on_task_removed:
                    self.on_task_removed(scene_no)

            elif msg_type == 'ALL_TASKS_REMOVED':
                # 확장앱에서 전체 작업 취소
                print(f"[GrokBridge] 전체 작업 취소 알림")
                # [Fix] 모든 작업 저장 경로 기록 정리
                self._task_target_folders.clear()
                if self.on_all_tasks_removed:
                    self.on_all_tasks_removed()

            elif msg_type == 'GET_PROJECT_PATH':
                # 확장앱에서 프로젝트 경로 요청
                await self._send_project_path_response(websocket)

            elif msg_type == 'PING':
                # 핑에 응답
                await websocket.send(json.dumps({'type': 'PONG'}))

            elif msg_type == 'VIDEO_DOWNLOADED':
                # 비디오 다운로드 완료 - 파일 이동
                print(f"[GrokBridge] VIDEO_DOWNLOADED 수신: scene={data.get('sceneNo')}, file={data.get('filePath', '')[:60]}")
                print(f"[GrokBridge] on_video_moved 콜백 설정 여부: {self.on_video_moved is not None}")
                await self._handle_video_downloaded(data)

            elif msg_type == 'WHISK_IMAGE_RESULT':
                # Whisk 이미지 생성 성공
                request_id = data.get('requestId')
                print(f"[GrokBridge] Whisk 이미지 생성 성공: requestId={request_id}")
                if request_id and request_id in self.pending_tasks:
                    self.pending_tasks[request_id].set_result(data)
                    del self.pending_tasks[request_id]

            elif msg_type == 'WHISK_IMAGE_FAILED':
                # Whisk 이미지 생성 실패
                request_id = data.get('requestId')
                error_code = data.get('errorCode', 'UNKNOWN')
                error_msg = data.get('error', 'Unknown error')
                print(f"[GrokBridge] Whisk 이미지 생성 실패: requestId={request_id}, errorCode={error_code}, error={error_msg}")
                if request_id and request_id in self.pending_tasks:
                    self.pending_tasks[request_id].set_result(data)  # 실패도 result로 처리 (exception 아닌 data에 error 포함)
                    del self.pending_tasks[request_id]

            elif msg_type == 'WHISK_UPLOAD_RESULT':
                # Whisk 참조 이미지 업로드 결과 (성공/실패 모두)
                request_id = data.get('requestId')
                if data.get('success'):
                    print(f"[GrokBridge] Whisk 이미지 업로드 성공: requestId={request_id}, mediaId={data.get('mediaId')}")
                else:
                    print(f"[GrokBridge] Whisk 이미지 업로드 실패: requestId={request_id}, error={data.get('error')}")
                if request_id and request_id in self.pending_tasks:
                    self.pending_tasks[request_id].set_result(data)
                    del self.pending_tasks[request_id]

            # ===== Flow 이미지 생성 응답 처리 =====
            elif msg_type == 'FLOW_IMAGE_RESULT':
                request_id = data.get('requestId')
                print(f"[GrokBridge] Flow 이미지 생성 성공: requestId={request_id}")
                if request_id and request_id in self.pending_tasks:
                    self.pending_tasks[request_id].set_result(data)
                    del self.pending_tasks[request_id]

            elif msg_type == 'FLOW_IMAGE_FAILED':
                request_id = data.get('requestId')
                error_code = data.get('errorCode', 'UNKNOWN')
                error_msg = data.get('error', 'Unknown error')
                print(f"[GrokBridge] Flow 이미지 생성 실패: requestId={request_id}, errorCode={error_code}, error={error_msg}")
                if request_id and request_id in self.pending_tasks:
                    self.pending_tasks[request_id].set_result(data)
                    del self.pending_tasks[request_id]

            elif msg_type == 'FLOW_ENSURE_TAB_RESULT':
                request_id = data.get('requestId')
                if data.get('success'):
                    print(f"[GrokBridge] Flow 탭 준비 완료: projectId={data.get('projectId')}")
                else:
                    print(f"[GrokBridge] Flow 탭 준비 실패: error={data.get('error')}")
                if request_id and request_id in self.pending_tasks:
                    self.pending_tasks[request_id].set_result(data)
                    del self.pending_tasks[request_id]

            elif msg_type == 'FLOW_UPLOAD_RESULT':
                request_id = data.get('requestId')
                if data.get('success'):
                    print(f"[GrokBridge] Flow 이미지 업로드 성공: requestId={request_id}, imageRef={data.get('imageRef')}")
                else:
                    print(f"[GrokBridge] Flow 이미지 업로드 실패: requestId={request_id}, error={data.get('error')}")
                if request_id and request_id in self.pending_tasks:
                    self.pending_tasks[request_id].set_result(data)
                    del self.pending_tasks[request_id]

            elif msg_type == 'REQUEST_RETRY_SCENE':
                # 확장앱에서 장면 재실행 요청 → MakeLens 앱에 새 데이터 요청
                retry_scene_raw = data.get('sceneNo')
                try:
                    retry_scene_no = int(retry_scene_raw)
                except (TypeError, ValueError):
                    retry_scene_no = 0

                print(f"[GrokBridge] 장면 재실행 요청 수신: scene={retry_scene_raw}")

                if retry_scene_no <= 0:
                    await self._send_to_all({
                        'type': 'RETRY_SCENE_DATA',
                        'sceneNo': retry_scene_raw,
                        'success': False,
                        'error': '유효하지 않은 장면 번호입니다.'
                    })
                    return

                if self.on_retry_scene_request:
                    self.on_retry_scene_request(retry_scene_no)
                else:
                    # 콜백 미등록 시 실패 응답
                    await self._send_to_all({
                        'type': 'RETRY_SCENE_DATA',
                        'sceneNo': retry_scene_no,
                        'success': False,
                        'error': '메이크렌즈 앱에서 재실행 기능을 지원하지 않습니다.'
                    })

            elif msg_type == 'OPEN_FOLDER':
                # 폴더 열기 요청
                await self._handle_open_folder(data)

            elif msg_type == 'OPEN_VIDEO_FILE':
                # 특정 씬의 영상 파일 열기 요청
                await self._handle_open_video_file(data)

        except json.JSONDecodeError as e:
            print(f"[GrokBridge] JSON 파싱 오류: {e}")
        except Exception as e:
            print(f"[GrokBridge] 메시지 처리 오류: {e}")

    async def _send_to_all(self, message: dict):
        """모든 연결된 클라이언트에게 메시지 전송 (확장앱 다중 탭/재연결 시 누락 방지)"""
        if not self.clients:
            print("[GrokBridge] 연결된 클라이언트 없음")
            return False

        message_str = json.dumps(message)
        sent = False
        stale_clients = []
        
        # 모든 클라이언트에게 전송 (break 제거)
        for client in list(self.clients):
            try:
                await client.send(message_str)
                sent = True
            except websockets.exceptions.ConnectionClosed:
                stale_clients.append(client)

        # stale 연결 정리
        for c in stale_clients:
            self.clients.discard(c)

        return sent

    async def set_folder_name(self, folder_name: str):
        """프로젝트 폴더명 설정 (파일명에 사용)"""
        await self._send_to_all({
            'type': 'SET_FOLDER_NAME',
            'folderName': folder_name
        })
        print(f"[GrokBridge] 폴더명 설정: {folder_name}")

    async def set_project_path(self, project_path: str, download_folder: str = None):
        """
        프로젝트 경로 설정 및 확장앱에 전송

        Args:
            project_path: 메이크렌즈 프로젝트 전체 경로 (예: C:/프로젝트/my_project)
            download_folder: 비디오 다운로드 폴더 경로 (미지정시 자동 생성)
        """
        self.project_path = project_path

        # 다운로드 폴더 경로 설정 (프로젝트/이미지 및 오디오/비디오)
        if download_folder:
            self.download_folder = download_folder
        else:
            self.download_folder = os.path.join(project_path, '이미지 및 오디오', '비디오')

        # 폴더가 없으면 생성
        if self.download_folder and not os.path.exists(self.download_folder):
            os.makedirs(self.download_folder, exist_ok=True)
            print(f"[GrokBridge] 비디오 폴더 생성: {self.download_folder}")

        # 확장앱에 경로 전송
        await self._send_to_all({
            'type': 'SET_PROJECT_PATH',
            'projectPath': self.project_path,
            'downloadFolder': self.download_folder
        })
        print(f"[GrokBridge] 프로젝트 경로 설정: {self.project_path}")
        print(f"[GrokBridge] 다운로드 폴더: {self.download_folder}")

    async def _send_project_path_response(self, websocket):
        """확장앱의 프로젝트 경로 요청에 응답"""
        response = {
            'type': 'PROJECT_PATH_RESPONSE',
            'success': bool(self.project_path),
            'projectPath': self.project_path,
            'downloadFolder': self.download_folder
        }
        await websocket.send(json.dumps(response))
        print(f"[GrokBridge] 프로젝트 경로 응답: {self.project_path}")

    async def _handle_open_folder(self, data):
        """폴더 열기 요청 처리"""
        import subprocess
        import sys

        folder_type = data.get('folderType', 'video')  # 'video' (Grok은 비디오만)

        # 비디오 폴더는 download_folder 직접 사용
        if folder_type == 'video' and self.download_folder:
            folder_path = self.download_folder
        elif self.project_path:
            # 폴백: project_path에서 경로 생성
            folder_path = os.path.join(self.project_path, '이미지 및 오디오', '비디오')
        else:
            print("[GrokBridge] 프로젝트 경로가 설정되지 않음")
            return

        # 폴더가 없으면 생성
        if not os.path.exists(folder_path):
            os.makedirs(folder_path, exist_ok=True)

        # 폴더 열기
        try:
            if sys.platform == 'win32':
                os.startfile(folder_path)
            elif sys.platform == 'darwin':
                subprocess.run(['open', folder_path])
            else:
                subprocess.run(['xdg-open', folder_path])
            print(f"[GrokBridge] 폴더 열기: {folder_path}")
        except Exception as e:
            print(f"[GrokBridge] 폴더 열기 실패: {e}")

    async def _handle_open_video_file(self, data):
        """특정 씬의 영상 파일 열기"""
        import subprocess
        import sys

        scene_no = data.get('sceneNo')
        if not scene_no:
            print("[GrokBridge] 씬 번호가 없음")
            return

        # 비디오 폴더 경로 결정
        video_folder = self.download_folder
        if not video_folder and self.project_path:
            video_folder = os.path.join(self.project_path, '이미지 및 오디오', '비디오')

        if not video_folder or not os.path.exists(video_folder):
            print(f"[GrokBridge] 비디오 폴더를 찾을 수 없음: {video_folder}")
            return

        # 해당 씬의 영상 파일 찾기 (scene_01_grok.mp4 형식)
        video_filename = f"scene_{str(scene_no).zfill(2)}_grok.mp4"
        video_path = os.path.join(video_folder, video_filename)

        if not os.path.exists(video_path):
            print(f"[GrokBridge] 영상 파일을 찾을 수 없음: {video_path}")
            return

        # 영상 파일 열기 (기본 동영상 플레이어로)
        try:
            if sys.platform == 'win32':
                os.startfile(video_path)
            elif sys.platform == 'darwin':
                subprocess.run(['open', video_path])
            else:
                subprocess.run(['xdg-open', video_path])
            print(f"[GrokBridge] 영상 파일 열기: {video_path}")
        except Exception as e:
            print(f"[GrokBridge] 영상 파일 열기 실패: {e}")

    async def _handle_video_downloaded(self, data):
        """
        비디오 다운로드 완료 처리
        크롬이 다운로드한 원본 파일 경로를 그대로 사용 (이동 없음)
        """
        import time

        source_path = data.get('filePath', '')
        scene_no = data.get('sceneNo')
        attempt_id = data.get('attemptId')
        video_url = data.get('videoUrl', '')

        print(f"[GrokBridge] VIDEO_DOWNLOADED 처리: scene={scene_no}, file={source_path[:60]}")

        # sceneNo가 없으면 pending_tasks에서 복구 시도
        if not scene_no:
            recovered_scene = None
            for tid in list(self.pending_tasks.keys()):
                parts = tid.split('_')
                if len(parts) >= 2 and parts[0] == 'task':
                    try:
                        candidate = int(parts[1])
                        if recovered_scene is None:
                            recovered_scene = candidate
                        else:
                            recovered_scene = None
                            break
                    except (ValueError, IndexError):
                        pass
            if recovered_scene:
                scene_no = recovered_scene
                print(f"[GrokBridge] ✅ sceneNo 복구: scene={scene_no}")
            else:
                print(f"[GrokBridge] sceneNo 없음, 무시")
                return

        def _move_to_target(src_path, s_no):
            """scene_01_grok.mp4 형식으로 복사하여 지정 폴더에 저장 (원본 유지)"""
            import shutil
            if not os.path.exists(src_path):
                return src_path
            folder = self._task_target_folders.get(s_no) or self.download_folder or os.path.dirname(src_path)
            target_name = f"scene_{str(s_no).zfill(2)}_grok.mp4"
            os.makedirs(folder, exist_ok=True)
            target = os.path.join(folder, target_name)
            # 이미 같은 경로면 이동 불필요
            if os.path.abspath(src_path) == os.path.abspath(target):
                return target
            try:
                # 원본 파일을 지우지 않고 복사만 수행하되, 
                # shutil.copy를 사용하여 생성 시간(mtime)을 현재로 갱신 (Synth에서 새 파일로 인식하도록 함)
                shutil.copy(src_path, target)
                return target
            except Exception as e:
                print(f"[GrokBridge] 파일 복사 실패: {e}")
            return src_path

        # 크롬 다운로드 파일 경로가 유효하면 rename 후 저장
        if source_path and os.path.exists(source_path):
            target_path = _move_to_target(source_path, scene_no)
            print(f"[GrokBridge] ✅ 파일 저장: {target_path}")
            self._send_progress(scene_no, 'done', f'✅ 완료: {os.path.basename(target_path)}')
            self._notify_video_moved(scene_no, attempt_id, source_path, target_path, True)
            return

        # 파일 경로가 없으면 크롬 다운로드 폴더에서 폴링
        self._send_progress(scene_no, 'chrome_search', '다운로드 파일 찾는 중...')
        source_path = await self._poll_chrome_download(time.time())

        if source_path and os.path.exists(source_path):
            target_path = _move_to_target(source_path, scene_no)
            print(f"[GrokBridge] ✅ 폴링으로 파일 저장: {target_path}")
            self._send_progress(scene_no, 'done', f'✅ 완료: {os.path.basename(target_path)}')
            self._notify_video_moved(scene_no, attempt_id, source_path, target_path, True)
        else:
            print(f"[GrokBridge] ❌ 파일을 찾을 수 없음")
            self._send_progress(scene_no, 'error', '❌ 다운로드 파일을 찾을 수 없음')
            self._notify_video_moved(scene_no, attempt_id, '', '', False, '파일을 찾을 수 없음')

    async def _try_direct_download(self, video_url, target_path, scene_no, attempt_id, max_retries=3):
        """videoUrl로 직접 다운로드 시도 (재시도 + 쓰기 방어 포함)"""
        import urllib.request
        import tempfile
        import shutil
        import time

        # target 폴더 쓰기 가능 여부 사전 체크
        target_dir = os.path.dirname(target_path)
        try:
            os.makedirs(target_dir, exist_ok=True)
            # 쓰기 테스트 (원드라이브 동기화 잠금 등 감지)
            test_file = os.path.join(target_dir, '.makelens_write_test')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except (PermissionError, OSError) as e:
            print(f"[GrokBridge] ⚠️ target 폴더 쓰기 불가: {target_dir} - {e}")
            # 3초 후 재시도 (원드라이브 동기화 잠금은 일시적)
            await asyncio.sleep(3)
            try:
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                print(f"[GrokBridge] target 폴더 쓰기 복구됨")
            except Exception:
                print(f"[GrokBridge] target 폴더 쓰기 여전히 불가, 폴백 진행")
                return False

        for attempt in range(1, max_retries + 1):
            try:
                print(f"[GrokBridge] 직접 다운로드 시도 ({attempt}/{max_retries}): {video_url[:80]}")

                # 임시 파일에 먼저 다운로드 (원드라이브 잠금/깨진 파일 방지)
                tmp_fd, tmp_path = tempfile.mkstemp(suffix='.mp4', dir=target_dir)
                os.close(tmp_fd)

                try:
                    req = urllib.request.Request(video_url, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })
                    with urllib.request.urlopen(req, timeout=120) as response:
                        with open(tmp_path, 'wb') as f:
                            while True:
                                chunk = response.read(1024 * 1024)  # 1MB 단위
                                if not chunk:
                                    break
                                f.write(chunk)

                    # 다운로드 검증
                    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
                        # 최종 위치로 이동 (같은 드라이브이므로 빠른 rename)
                        if os.path.exists(target_path):
                            os.remove(target_path)
                        shutil.move(tmp_path, target_path)

                        file_size = os.path.getsize(target_path)
                        size_mb = file_size / (1024 * 1024)
                        print(f"[GrokBridge] ✅ 직접 다운로드 성공: {target_path} ({file_size:,} bytes)")
                        self._send_progress(scene_no, 'done', f'✅ 저장 완료: {os.path.basename(target_path)} ({size_mb:.1f}MB)')
                        self._notify_video_moved(scene_no, attempt_id, video_url, target_path, True)
                        return True
                    else:
                        print(f"[GrokBridge] ⚠️ 다운로드 파일 크기 이상, 재시도...")

                finally:
                    # 임시 파일 정리
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass

            except Exception as e:
                print(f"[GrokBridge] 직접 다운로드 실패 ({attempt}/{max_retries}): {e}")
                self._send_progress(scene_no, 'direct_download_retry', f'다운로드 재시도 ({attempt}/{max_retries})...')
                if attempt < max_retries:
                    wait = attempt * 3  # 3초, 6초, 9초
                    print(f"[GrokBridge] {wait}초 후 재시도...")
                    await asyncio.sleep(wait)

        print(f"[GrokBridge] 직접 다운로드 {max_retries}회 모두 실패, 크롬 다운로드 폴백")
        return False

    async def _poll_chrome_download(self, start_time):
        """크롬 다운로드 폴더에서 최신 mp4 파일 폴링"""
        import glob
        import time

        search_paths = []
        home = os.path.expanduser('~')

        # 0) 사용자 지정 다운로드 폴더 최우선 검색
        if self.download_folder and os.path.exists(self.download_folder):
            search_paths.append(self.download_folder)

        # 1) Chrome 자체 설정에서 다운로드 경로 읽기 (사용자가 변경한 경로 대응)
        try:
            import json as _json
            chrome_profiles = [
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'User Data', 'Default'),
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'User Data', 'Profile 1'),
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'User Data', 'Profile 2'),
            ]
            for profile_dir in chrome_profiles:
                prefs_file = os.path.join(profile_dir, 'Preferences')
                if os.path.exists(prefs_file):
                    with open(prefs_file, 'r', encoding='utf-8') as f:
                        prefs = _json.load(f)
                    chrome_dl = prefs.get('download', {}).get('default_directory', '')
                    if chrome_dl and os.path.exists(chrome_dl) and chrome_dl not in search_paths:
                        search_paths.insert(0, chrome_dl)
                        print(f"[GrokBridge] Chrome 설정에서 다운로드 경로 발견: {chrome_dl}")
                        break
        except Exception as chrome_err:
            print(f"[GrokBridge] Chrome 설정 읽기 실패 (무시): {chrome_err}")

        # 2) Windows 레지스트리에서 시스템 다운로드 폴더 확인
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                dl_path = winreg.QueryValueEx(key, '{374DE290-123F-4565-9164-39C4925E467B}')[0]
                dl_path = os.path.expandvars(dl_path)
                if os.path.exists(dl_path) and dl_path not in search_paths:
                    search_paths.insert(0 if not search_paths else 1, dl_path)
        except Exception:
            pass

        # 3) 기본 폴더 후보들
        for folder_name_candidate in ['Downloads', 'Download', 'downloads', 'Desktop', 'desktop', '다운로드', '바탕화면']:
            candidate_path = os.path.join(home, folder_name_candidate)
            if os.path.exists(candidate_path) and candidate_path not in search_paths:
                search_paths.append(candidate_path)

        print(f"[GrokBridge] 크롬 파일 검색 경로: {search_paths}")

        POLL_MAX = 60
        POLL_INTERVAL = 3
        poll_start = time.time()

        while time.time() - poll_start < POLL_MAX:
            candidates = []
            for search_path in search_paths:
                candidates += glob.glob(os.path.join(search_path, '*grok*.mp4'))
                candidates += glob.glob(os.path.join(search_path, '*.mp4'))

            candidates = [c for c in candidates if not c.endswith('.crdownload')]
            seen = set()
            candidates = [c for c in candidates if not (c in seen or seen.add(c))]

            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                newest = candidates[0]
                file_age = time.time() - os.path.getmtime(newest)
                if file_age < 300:
                    print(f"[GrokBridge] 다운로드 폴더에서 파일 발견: {newest} (경과: {file_age:.1f}s)")
                    return newest

            elapsed = time.time() - poll_start
            print(f"[GrokBridge] 파일 폴링 중... ({elapsed:.0f}s/{POLL_MAX}s)")
            await asyncio.sleep(POLL_INTERVAL)

        return None

    def _send_progress(self, scene_no, step, message):
        """사이드패널에 진행 상태 메시지 전송"""
        asyncio.ensure_future(self._send_to_all({
            'type': 'DOWNLOAD_PROGRESS',
            'sceneNo': scene_no,
            'step': step,
            'message': message
        }))

    def send_whisk_progress(self, scene_no, message):
        """Whisk 이미지 생성 진행 상태를 사이드패널에 전송"""
        try:
            coro = self._send_to_all({
                'type': 'WHISK_PROGRESS',
                'sceneNo': scene_no,
                'message': message
            })
            # [Fix] 워커 스레드에서 호출되므로 run_coroutine_threadsafe 사용
            try:
                loop = asyncio.get_running_loop()
                # 이미 async 컨텍스트면 ensure_future
                asyncio.ensure_future(coro, loop=loop)
            except RuntimeError:
                # 다른 스레드에서 호출 → 서버의 이벤트 루프에 전송
                if self.server and hasattr(self.server, '_loop') and self.server._loop:
                    asyncio.run_coroutine_threadsafe(coro, self.server._loop)
                elif hasattr(self, '_loop') and self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(coro, self._loop)
                else:
                    coro.close()  # 이벤트 루프 없으면 정리
        except Exception:
            pass  # 연결 안 되어도 무시

    def send_flow_progress(self, scene_no, message):
        """Flow 이미지 생성 진행 상태를 사이드패널에 전송 (send_whisk_progress와 동일 패턴)"""
        try:
            coro = self._send_to_all({
                'type': 'FLOW_PROGRESS',
                'sceneNo': scene_no,
                'message': message
            })
            # [Fix] 워커 스레드에서 호출되므로 run_coroutine_threadsafe 사용
            try:
                loop = asyncio.get_running_loop()
                asyncio.ensure_future(coro, loop=loop)
            except RuntimeError:
                if self.server and hasattr(self.server, '_loop') and self.server._loop:
                    asyncio.run_coroutine_threadsafe(coro, self.server._loop)
                elif hasattr(self, '_loop') and self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(coro, self._loop)
                else:
                    coro.close()
        except Exception:
            pass

    def _notify_video_moved(self, scene_no, attempt_id, source_path, target_path, success, error=None):
        """비디오 이동/다운로드 완료 콜백 호출 + 확장앱에 완료 신호 전송"""
        # [Fix] 완료된 작업의 저장 경로 기록 정리
        if scene_no and scene_no in self._task_target_folders:
            del self._task_target_folders[scene_no]

        if self.on_video_moved:
            result = {
                'type': 'VIDEO_MOVED',
                'sceneNo': scene_no,
                'attemptId': attempt_id,
                'sourcePath': source_path,
                'targetPath': target_path,
                'success': success
            }
            if error:
                result['error'] = error
            self.on_video_moved(result)
            print(f"[GrokBridge] on_video_moved 콜백: success={success}, target={target_path}")

        # 확장앱에 파일 준비 완료 신호 전송 (확장앱이 이걸 받아야 다음 장면 시작)
        asyncio.ensure_future(self._send_to_all({
            'type': 'VIDEO_MOVE_COMPLETE',
            'sceneNo': scene_no,
            'attemptId': attempt_id,
            'targetPath': target_path,
            'success': success
        }))

    async def cancel_all_tasks(self):
        """확장앱에 전체 작업 취소 요청 (즉시 중지)"""
        self._task_target_folders.clear()  # [Fix] 저장 경로 기록 정리
        # [Fix] 확장앱에 먼저 CANCEL_ALL을 전송하여 fetch를 즉시 abort
        await self._send_to_all({'type': 'CANCEL_ALL'})
        # 그 다음 Python 측 pending futures 정리
        cancelled_count = 0
        for task_id in list(self.pending_tasks.keys()):
            if not (task_id.startswith('whisk_') or task_id.startswith('whisk_upload_') or task_id.startswith('flow_') or task_id.startswith('flow_upload_')):
                continue
            future = self.pending_tasks.get(task_id)
            if future is None:
                continue
            if not future.done():
                try:
                    future.set_result({
                        'success': False,
                        'error': 'User requested stop',
                        'errorCode': 'USER_CANCELLED'
                    })
                    cancelled_count += 1
                except Exception:
                    pass
            self.pending_tasks.pop(task_id, None)
        print(f"[GrokBridge] 전체 작업 취소 완료 (whisk/flow pending 취소: {cancelled_count})")

    async def cancel_task(self, task_id: str):
        """확장앱에 개별 작업 취소 요청"""
        await self._send_to_all({'type': 'CANCEL_TASK', 'taskId': task_id})
        # pending_tasks에서도 제거
        if task_id in self.pending_tasks:
            self.pending_tasks[task_id].cancel()
            del self.pending_tasks[task_id]
        print(f"[GrokBridge] 작업 취소 요청 전송: {task_id}")

    async def set_max_parallel(self, count: int):
        """최대 병렬 작업 수 설정 (1~5)"""
        await self._send_to_all({
            'type': 'SET_MAX_PARALLEL',
            'count': min(5, max(1, count))
        })
        print(f"[GrokBridge] 최대 병렬 작업 수: {count}")

    async def update_settings(self, settings: dict):
        """확장앱 런타임 설정 변경 (continueOnError, maxRetryCount, autoDownload 등)

        Args:
            settings: 변경할 설정 dict. 허용 키:
                - continueOnError (bool): 실패 시 다음 작업 계속 진행
                - maxRetryCount (int): 일시적 에러 자동 재시도 최대 횟수
                - autoDownload (bool): 자동 다운로드 여부
                - upscaleBeforeDownload (bool): 다운로드 전 업스케일 여부
        """
        allowed = {'continueOnError', 'maxRetryCount', 'autoDownload', 'upscaleBeforeDownload'}
        filtered = {k: v for k, v in settings.items() if k in allowed}
        if not filtered:
            print("[GrokBridge] update_settings: 유효한 설정 키 없음")
            return
        await self._send_to_all({
            'type': 'SET_SETTINGS',
            'settings': filtered
        })
        print(f"[GrokBridge] 설정 업데이트: {filtered}")

    async def resume_queue(self):
        """실패로 멈춘 대기열 이어서 실행"""
        self.queue_paused = False
        await self._send_to_all({'type': 'RESUME_QUEUE'})
        print("[GrokBridge] 대기열 이어서 실행 요청")

    async def send_queue_preview(self, scenes_info: list, is_new_run: bool = True):
        """
        확장앱 대기열에 미리보기 전송 (전체영상 생성 시 모든 장면을 한꺼번에 보여주기)
        QUEUE_PREVIEW 타입으로 전송하여 대기열 UI에만 표시
        ※ 이미지는 제외하고 메타데이터만 전송 (크기 제한 방지)

        Args:
            scenes_info: [{'sceneNo': int, 'prompt': str, 'imageBase64': str}, ...] 리스트
            is_new_run: True면 completedTasks 초기화 (새 실행), False면 유지 (추가 배치)
        """
        print(f"[GrokBridge] send_queue_preview 호출: {len(scenes_info)}개 장면, is_new_run={is_new_run}, clients: {len(self.clients)}")
        if not self.clients:
            print("[GrokBridge] 연결된 클라이언트 없음 - 미리보기 전송 생략")
            return

        # 이미지 제외하고 메타데이터만 전송
        preview_items = []
        for info in scenes_info:
            preview_items.append({
                'sceneNo': info['sceneNo'],
                'prompt': info.get('prompt', ''),
                # imageBase64 제외!
                'folderName': info.get('folderName', '')
            })

        await self._send_to_all({
            'type': 'QUEUE_PREVIEW',
            'items': preview_items,
            'totalCount': len(preview_items),
            'isNewRun': is_new_run
        })
        print(f"[GrokBridge] 대기열 미리보기 전송: {len(preview_items)}개 장면 (이미지 제외, isNewRun={is_new_run})")

    async def send_retry_scene_data(self, scene_no: int, prompt: str, image_base64: str, folder_name: str = ''):
        """
        재실행용 장면 데이터를 확장앱에 전송

        Args:
            scene_no: 장면 번호
            prompt: 동영상 프롬프트
            image_base64: Base64 이미지 데이터
            folder_name: 프로젝트 폴더명
        """
        # [Fix] 재실행 작업의 저장 경로도 기억
        if self.download_folder:
            self._task_target_folders[scene_no] = self.download_folder

        await self._send_to_all({
            'type': 'RETRY_SCENE_DATA',
            'sceneNo': scene_no,
            'prompt': prompt,
            'imageBase64': image_base64,
            'folderName': folder_name,
            'success': True
        })
        print(f"[GrokBridge] 재실행 장면 데이터 전송: scene={scene_no}, prompt={len(prompt)}자")

    async def send_retry_scene_error(self, scene_no: int, error: str):
        """재실행 실패 응답 전송"""
        await self._send_to_all({
            'type': 'RETRY_SCENE_DATA',
            'sceneNo': scene_no,
            'success': False,
            'error': error
        })
        print(f"[GrokBridge] 재실행 실패 전송: scene={scene_no}, error={error}")

    async def add_all_tasks(self, tasks_info: list):
        """
        모든 작업을 순차적으로 대기열에 추가 (전체영상 생성 시 사용)
        ※ 크기 제한 방지를 위해 1개씩 ADD_TASK로 전송

        Args:
            tasks_info: [{'sceneNo': int, 'prompt': str, 'imageBase64': str, 'folderName': str}, ...]
        """
        print(f"[GrokBridge] add_all_tasks 호출: {len(tasks_info)}개 작업")
        if not self.clients:
            print("[GrokBridge] 연결된 클라이언트 없음 - 작업 추가 불가")
            return

        import time
        for i, info in enumerate(tasks_info):
            task_id = f"task_{info['sceneNo']}_{int(time.time() * 1000)}_{i}"
            attempt_id = f"att_{info['sceneNo']}_{int(time.time() * 1000)}_{i}"

            # [Fix] 작업 생성 시점의 download_folder를 기억 (프로젝트 전환 대비)
            scene_no = info['sceneNo']
            if self.download_folder and scene_no not in self._task_target_folders:
                self._task_target_folders[scene_no] = self.download_folder

            # Future 생성 (완료/실패 추적용 - await하지 않음)
            future = asyncio.get_event_loop().create_future()
            # add_all_tasks는 future를 await하지 않으므로 (콜백 기반 처리)
            # TASK_FAILED 시 set_exception() 경고 방지
            future.add_done_callback(lambda f: f.exception() if f.done() and not f.cancelled() else None)
            self.pending_tasks[task_id] = future

            # 1개씩 ADD_TASK로 전송
            await self._send_to_all({
                'type': 'ADD_TASK',
                'task': {
                    'id': task_id,
                    'sceneNo': info['sceneNo'],
                    'attemptId': attempt_id,
                    'prompt': info.get('prompt', ''),
                    'imageBase64': info.get('imageBase64', ''),
                    'folderName': info.get('folderName', ''),
                    'duration': info.get('duration', '6s')
                }
            })
            print(f"[GrokBridge] 작업 {i+1}/{len(tasks_info)} 전송: 씬 {info['sceneNo']}")

            # 약간의 딜레이 (확장앱이 처리할 시간)
            await asyncio.sleep(0.1)

        print(f"[GrokBridge] {len(tasks_info)}개 작업 순차 전송 완료")

    async def upload_whisk_image(
        self,
        image_base64: str,
        category: str = 'MEDIA_CATEGORY_SUBJECT',
        with_caption: bool = True,
        timeout: float = 60.0
    ) -> Dict[str, Any]:
        """
        Whisk 참조 이미지 업로드 → mediaId 발급

        Args:
            image_base64: 이미지 base64 데이터 (data:image/...;base64,... 형식도 가능)
            category: 'MEDIA_CATEGORY_SUBJECT' / 'MEDIA_CATEGORY_SCENE' / 'MEDIA_CATEGORY_STYLE'
            with_caption: True이면 자동 캡션도 생성
            timeout: 타임아웃 (초)

        Returns:
            {'success': bool, 'mediaId': str, 'caption': str, 'error': str, 'errorCode': str}
        """
        if not self.clients:
            return {'success': False, 'error': '확장프로그램 연결 안됨', 'errorCode': 'NO_CONNECTION'}

        import uuid
        request_id = f"whisk_upload_{uuid.uuid4().hex[:8]}"

        # Future 생성
        future = self._loop.create_future() if hasattr(self, '_loop') and self._loop else asyncio.get_event_loop().create_future()
        self.pending_tasks[request_id] = future

        # 메시지 전송
        message = {
            'type': 'WHISK_UPLOAD_IMAGE',
            'requestId': request_id,
            'imageBase64': image_base64,
            'category': category,
            'withCaption': with_caption
        }

        sent = await self._send_to_all(message)
        if not sent:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': '메시지 전송 실패', 'errorCode': 'NO_CONNECTION'}

        # 응답 대기
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': f'타임아웃 ({timeout}초)', 'errorCode': 'TIMEOUT'}
        except Exception as e:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': str(e), 'errorCode': 'API_ERROR'}

    async def generate_whisk_image(
        self,
        prompt: str,
        aspect_ratio: str = '16:9',
        reference_images: list = None,
        timeout: float = 120.0
    ) -> Dict[str, Any]:
        """
        Whisk API를 통한 이미지 생성 요청

        Args:
            prompt: 이미지 프롬프트
            aspect_ratio: 비율 ('1:1', '16:9', '9:16')
            reference_images: 참조 이미지 배열 [{category, mediaId, caption}]
            timeout: 타임아웃 (초)

        Returns:
            {'success': bool, 'imageBase64': str, 'error': str, 'errorCode': str}
        """
        if not self.clients:
            return {'success': False, 'error': '확장프로그램 연결 안됨', 'errorCode': 'NO_CONNECTION'}

        import uuid
        request_id = f"whisk_{uuid.uuid4().hex[:8]}"

        # Future 생성
        future = self._loop.create_future() if hasattr(self, '_loop') and self._loop else asyncio.get_event_loop().create_future()
        self.pending_tasks[request_id] = future

        # 메시지 전송
        message = {
            'type': 'WHISK_GENERATE_IMAGE',
            'requestId': request_id,
            'prompt': prompt,
            'aspectRatio': aspect_ratio,
            'referenceImages': reference_images or []
        }

        sent = await self._send_to_all(message)
        if not sent:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': '메시지 전송 실패', 'errorCode': 'NO_CONNECTION'}

        # 응답 대기
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': f'타임아웃 ({timeout}초)', 'errorCode': 'TIMEOUT'}
        except Exception as e:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': str(e), 'errorCode': 'API_ERROR'}

    # ===== Flow 이미지 생성 비동기 메서드 =====

    async def upload_flow_image(
        self,
        image_base64: str,
        timeout: float = 60.0
    ) -> Dict[str, Any]:
        """
        Flow 참조 이미지 업로드

        Args:
            image_base64: base64 인코딩 이미지 (data:image/png;base64,... 또는 raw)
            timeout: 타임아웃 (초)

        Returns:
            {'success': bool, 'imageRef': str, 'error': str, 'errorCode': str}
        """
        if not self.clients:
            return {'success': False, 'error': '확장프로그램 연결 안됨', 'errorCode': 'NO_CONNECTION'}

        import uuid
        request_id = f"flow_upload_{uuid.uuid4().hex[:8]}"

        future = self._loop.create_future() if hasattr(self, '_loop') and self._loop else asyncio.get_event_loop().create_future()
        self.pending_tasks[request_id] = future

        message = {
            'type': 'FLOW_UPLOAD_IMAGE',
            'requestId': request_id,
            'imageBase64': image_base64
        }

        sent = await self._send_to_all(message)
        if not sent:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': '메시지 전송 실패', 'errorCode': 'NO_CONNECTION'}

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': f'타임아웃 ({timeout}초)', 'errorCode': 'TIMEOUT'}
        except Exception as e:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': str(e), 'errorCode': 'API_ERROR'}

    async def ensure_flow_tab(self, timeout: float = 60.0) -> Dict[str, Any]:
        """Flow 탭 사전 준비 (브라우저 탭 생성 + 인증 + projectId 확인)

        Returns:
            {'success': bool, 'projectId': str, 'error': str, 'errorCode': str}
        """
        if not self.clients:
            return {'success': False, 'error': '확장프로그램 연결 안됨', 'errorCode': 'NO_CONNECTION'}

        import uuid
        request_id = f"flow_ensure_{uuid.uuid4().hex[:8]}"

        future = self._loop.create_future() if hasattr(self, '_loop') and self._loop else asyncio.get_event_loop().create_future()
        self.pending_tasks[request_id] = future

        message = {
            'type': 'FLOW_ENSURE_TAB',
            'requestId': request_id
        }
        await self._send_to_all(message)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': f'Flow 탭 준비 타임아웃 ({timeout}초)', 'errorCode': 'TIMEOUT'}
        except Exception as e:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': str(e), 'errorCode': 'API_ERROR'}

    async def generate_flow_image(
        self,
        prompt: str,
        aspect_ratio: str = '16:9',
        reference_images: list = None,
        image_inputs: list = None,
        timeout: float = 120.0
    ) -> Dict[str, Any]:
        """
        Flow API를 통한 이미지 생성 요청

        Args:
            prompt: 이미지 프롬프트
            aspect_ratio: 비율 ('16:9', '9:16')
            reference_images: 참조 이미지 배열
            image_inputs: 에셋 참조 배열 [{"imageInputType": "IMAGE_INPUT_TYPE_REFERENCE", "name": "<uuid>"}]
            timeout: 타임아웃 (초)

        Returns:
            {'success': bool, 'imageBase64': str, 'assetName': str, 'error': str, 'errorCode': str}
        """
        if not self.clients:
            return {'success': False, 'error': '확장프로그램 연결 안됨', 'errorCode': 'NO_CONNECTION'}

        import uuid
        request_id = f"flow_{uuid.uuid4().hex[:8]}"

        future = self._loop.create_future() if hasattr(self, '_loop') and self._loop else asyncio.get_event_loop().create_future()
        self.pending_tasks[request_id] = future

        message = {
            'type': 'FLOW_GENERATE_IMAGE',
            'requestId': request_id,
            'prompt': prompt,
            'aspectRatio': aspect_ratio,
            'referenceImages': reference_images or [],
            'imageInputs': image_inputs or []
        }

        sent = await self._send_to_all(message)
        if not sent:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': '메시지 전송 실패', 'errorCode': 'NO_CONNECTION'}

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': f'타임아웃 ({timeout}초)', 'errorCode': 'TIMEOUT'}
        except Exception as e:
            if request_id in self.pending_tasks:
                del self.pending_tasks[request_id]
            return {'success': False, 'error': str(e), 'errorCode': 'API_ERROR'}

    async def generate_video(
        self,
        scene_no: int,
        prompt: str,
        image_path: Optional[str] = None,
        image_pil=None,
        folder_name: Optional[str] = None,
        timeout: float = 660.0
    ) -> Dict[str, Any]:
        """
        Image-to-Video 생성 요청

        Args:
            scene_no: 씬 번호
            prompt: 동영상 프롬프트
            image_path: 이미지 파일 경로 (선택)
            image_pil: PIL Image 객체 (선택)
            folder_name: 프로젝트 폴더명 (파일명에 사용)
            timeout: 타임아웃 (초) - 확장앱 10분 + 여유 1분

        Returns:
            {'taskId': str, 'sceneNo': int, 'videoUrl': str}
        """
        if not self.clients:
            raise ConnectionError("확장프로그램이 연결되지 않았습니다.")

        # 이미지를 Base64로 변환
        image_base64 = None

        if image_pil:
            # PIL Image -> Base64
            buffer = BytesIO()
            image_pil.save(buffer, format='PNG')
            image_base64 = f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"

        elif image_path and os.path.exists(image_path):
            # 파일 -> Base64
            with open(image_path, 'rb') as f:
                ext = os.path.splitext(image_path)[1].lower()
                mime = 'image/png' if ext == '.png' else 'image/jpeg'
                image_base64 = f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"

        # 작업 ID 생성
        import time
        task_id = f"task_{scene_no}_{int(time.time() * 1000)}"
        attempt_id = f"att_{scene_no}_{int(time.time() * 1000)}"

        # [Fix] 작업 생성 시점의 download_folder를 기억 (프로젝트 전환 대비)
        if self.download_folder:
            self._task_target_folders[scene_no] = self.download_folder

        # Future 생성
        future = asyncio.get_event_loop().create_future()
        self.pending_tasks[task_id] = future

        # 작업 전송
        task = {
            'type': 'ADD_TASK',
            'task': {
                'id': task_id,
                'sceneNo': scene_no,
                'attemptId': attempt_id,
                'prompt': prompt,
                'imageBase64': image_base64,
                'folderName': folder_name or ''
            }
        }

        await self._send_to_all(task)

        # 결과 대기
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            if task_id in self.pending_tasks:
                del self.pending_tasks[task_id]
            raise TimeoutError(f"작업 시간 초과 ({timeout}초)")

    async def generate_videos_batch(
        self,
        tasks: list,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> list:
        """
        여러 작업을 배치로 처리

        Args:
            tasks: [{'scene_no': int, 'prompt': str, 'image_pil': Image}] 리스트
            progress_callback: 진행률 콜백 (현재, 전체)

        Returns:
            결과 리스트
        """
        results = []
        total = len(tasks)

        for i, task in enumerate(tasks):
            try:
                result = await self.generate_video(
                    scene_no=task['scene_no'],
                    prompt=task['prompt'],
                    image_pil=task.get('image_pil'),
                    image_path=task.get('image_path')
                )
                results.append({
                    'success': True,
                    'scene_no': task['scene_no'],
                    'video_url': result.get('videoUrl')
                })
            except Exception as e:
                results.append({
                    'success': False,
                    'scene_no': task['scene_no'],
                    'error': str(e)
                })

            if progress_callback:
                progress_callback(i + 1, total)

        return results

    def get_status(self):
        """현재 상태 반환"""
        return {
            'is_running': self.is_running,
            'connected_clients': len(self.clients),
            'pending_tasks': len(self.pending_tasks)
        }

    @property
    def is_connected(self) -> bool:
        """확장프로그램 연결 여부"""
        return len(self.clients) > 0


# 싱글톤 인스턴스
_bridge_instance: Optional[GrokBridge] = None


def get_bridge(port: int = 9876) -> GrokBridge:
    """GrokBridge 싱글톤 인스턴스 반환"""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = GrokBridge(port)
    return _bridge_instance


# === 메이크렌즈 통합 예시 ===

async def example_usage():
    """사용 예시"""

    bridge = get_bridge()

    # 콜백 설정
    bridge.on_connected = lambda: print("확장프로그램 연결됨!")
    bridge.on_task_completed = lambda data: print(f"완료: 씬 {data['sceneNo']}")
    bridge.on_task_failed = lambda data: print(f"실패: {data['error']}")

    # 서버 시작
    await bridge.start()

    print("확장프로그램 연결을 기다리는 중...")
    print("Chrome에서 MakeLens 확장프로그램을 열고 '연결하기'를 클릭하세요.")

    # 연결 대기
    while not bridge.is_connected:
        await asyncio.sleep(1)

    print("연결됨! 작업을 시작합니다.")

    # 비디오 생성 요청 (예시)
    try:
        result = await bridge.generate_video(
            scene_no=1,
            prompt="A gentle breeze moves through the scene",
            image_path="test_image.png"  # 테스트 이미지 경로
        )
        print(f"생성 완료: {result}")
    except Exception as e:
        print(f"생성 실패: {e}")

    # 서버 유지
    while True:
        await asyncio.sleep(1)


if __name__ == '__main__':
    asyncio.run(example_usage())
