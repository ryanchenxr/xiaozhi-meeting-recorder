"""Dedicated persistent website session. Never connects to daily Chrome or localhost."""
import asyncio
import json
import threading
from pathlib import Path
from playwright.async_api import async_playwright
from store import GateError, atomic, now
import official

ROOT = Path(__file__).resolve().parent
SITE_CODE = (ROOT / 'site-worker.js').read_text()
ERRORS = {
 'needs_login': '需要重新登录；任务和原音已保留。若创建已提交，只允许读回原任务。',
 'upload_unknown': '上传结果未知，保留file_id和原音，禁止自动重传或重建。',
 'submission_unknown': '创建或处理结果未知，已保留请求身份；有note_id时只读回原任务。',
 'stream_unknown': '处理流结束但结果未知，只允许读回原任务。',
 'local_or_client_changed': '本地转换或官网客户端不兼容，保留原音，请查看诊断状态。',
 'upload_protocol': '上传协议与已验证格式不符，已停止。',
 'upload_rejected': 'OSS拒绝上传，保留现场，不自动重传。',
 'task_rejected': '处理请求被拒绝，不重复创建。',
 'duration_limit': '单个音频不得超过3小时。',
 'missing_file': '音频文件未成功传入专用浏览器。',
}

class BrowserWorker:
    def __init__(self, store):
        self.store = store
        self.login = {'state': 'starting'}
        self.pending = set()
        self.force_login_expiry = None
        self.guard = threading.Lock()
        self.started = threading.Event()
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        self.started.wait(3)

    def _thread(self):
        asyncio.run(self._main())

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self.started.set()
        try:
            async with async_playwright() as pw:
                profile = self.store.root / 'browser-profile'
                profile.mkdir(mode=0o700, exist_ok=True)
                self.context = await pw.chromium.launch_persistent_context(str(profile), channel='chrome', headless=False, chromium_sandbox=True, accept_downloads=False, no_viewport=True, args=['--window-size=1100,850'])
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                # Do not attach console/network/HAR loggers: all may include secrets.
                await self.page.expose_function('meetingBridgeCheckpoint', self._checkpoint)
                await self.page.goto('https://www.biji.com/note', wait_until='domcontentloaded', timeout=45000)
                await self._login_status()
                for task in self.store.tasks():
                    if task.get('note_id') and task.get('restart_read_required'):
                        self.enqueue(task['id'])
                while not self.stop_event.is_set():
                    try:
                        task_id = await asyncio.wait_for(self.queue.get(), timeout=3)
                    except asyncio.TimeoutError:
                        await self._login_status()
                        continue
                    try:
                        await self._process(task_id)
                    finally:
                        with self.guard:
                            self.pending.discard(task_id)
                        self.queue.task_done()
                    await self._login_status()
                await self.context.close()  # Only this launched context; daily Chrome untouched.
        except Exception as e:
            self.login = {'state': 'browser_unavailable', 'error_type': type(e).__name__, 'message': '专用浏览器不可用，请正常关闭后重启本程序；不会连接日常Chrome或更换通道。'}
            # An abruptly closed browser still leaves saved task intents intact.
            for task_id in list(self.pending):
                self._failure(task_id, 'submission_unknown')
        finally:
            self.stopped = True

    async def _login_status(self):
        try:
            result = await self.page.evaluate('''() => {
              if(!window.webpackChunkiget_biji)return null;
              let r;window.webpackChunkiget_biji.push([['mb_login_'+Date.now()],{},x=>{r=x}]);
              const m=r(48694).b3;
              return m.getTokenInfo();
            }''')
            if not result:
                self.login = {'state': 'client_loading'}
                return
            blocked = self.force_login_expiry is not None and self.force_login_expiry == result['tokenExpireAt']
            if self.force_login_expiry is not None and not blocked:
                self.force_login_expiry = None
            self.login = {'state': 'ready' if result['isLoginValid'] and not blocked else 'needs_login', **result}
        except Exception:
            self.login = {'state': 'browser_unavailable', 'message': '专用官网页面不可用，请保留任务后重启程序'}

    def enqueue(self, task_id):
        self.store.load(task_id)
        with self.guard:
            if task_id in self.pending:
                return False
            self.pending.add(task_id)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, task_id)
        return True

    async def _checkpoint(self, task_id, phase, data):
        if task_id not in self.pending:
            raise GateError('未授权任务回调')
        with self.store.lock:
            task = self.store.load(task_id)
            task['status'] = 'running'
            task['phase'] = phase
            task.setdefault('events', []).append({'phase': phase, 'at': now()})
            if phase in ('token_intent', 'put_intent'):
                task['remote_started'] = True
            elif phase == 'converted':
                task['converted'] = {k: data[k] for k in ('bytes', 'duration_ms', 'md5')}
                task.setdefault('timings', {}).update(init_ms=data['init_ms'], conversion_ms=data['conversion_ms'])
            elif phase == 'token_received':
                task['file_id'] = str(data['file_id'])
                task['upload'] = {'is_uploaded': bool(data['is_uploaded']), 'put_occurred': False}
            elif phase == 'put_received':
                task['upload'].update(put_occurred=True, put_http=int(data['http']), put_bytes=int(data['bytes']))
                task.setdefault('timings', {})['put_ms'] = data['ms']
            elif phase == 'submit_intent':
                self.store.reserve_submission(task_id)
                task['submit_started'] = True
            elif phase == 'submit_received':
                task['submit_http'] = int(data['http'])
            elif phase == 'note_id':
                if not str(data['note_id']).isdigit():
                    raise GateError('服务未返回字符串笔记身份')
                if task.get('note_id') and task['note_id'] != str(data['note_id']):
                    raise GateError('笔记身份不一致')
                task['note_id'] = str(data['note_id'])
                task.setdefault('timings', {}).setdefault('note_id_ms', data['ms'])
            elif phase == 'transcript':
                self.store.text_once(task_id, 'transcript.txt', '\n'.join(s['text'] for s in data['sentences']) + '\n')
                p = self.store.directory(task_id) / 'transcript.json'
                if not p.exists():
                    atomic(p, {'task_status': data['task_status'], 'sentences': data['sentences']})
                task.setdefault('timings', {})['transcript_ms'] = data['ms']
            elif phase == 'summary_started':
                task.setdefault('timings', {})['summary_first_ms'] = data['ms']
            elif phase == 'result':
                nid = str(data['note_id'])
                if task.get('note_id') and task['note_id'] != nid:
                    raise GateError('最终笔记身份不一致')
                task['note_id'] = nid
                self.store.text_once(task_id, 'summary.md', data['content'])
                task['title'] = data['title'][:250]
                task.setdefault('timings', {}).update(final_ms=data['ms'], total_ms=data['total_ms'])
            elif phase not in ('converting',):
                raise GateError('未知处理阶段')
            self.store.save(task)  # fsync before JS proceeds to the next side effect/event.
        return True

    def _failure(self, task_id, code):
        with self.store.lock:
            task = self.store.load(task_id)
            task['error'] = ERRORS.get(code, '处理停止，已保留原任务。')
            if code == 'needs_login':
                task['status'] = 'needs_login'
                self.force_login_expiry = self.login.get('tokenExpireAt', 0)
                self.login = {'state': 'needs_login'}
            elif task.get('note_id'):
                task['status'] = 'recovery_required'
            elif task.get('remote_started') or task.get('submit_started'):
                task['status'] = 'unknown'
            else:
                task['status'] = 'local_failed'
            self.store.save(task)

    async def _process(self, task_id):
        task = self.store.load(task_id)
        if task.get('note_id'):
            await self._read_and_share(task_id)
            return
        if task.get('submit_started') or task.get('remote_started'):
            self._failure(task_id, 'submission_unknown')
            return
        await self._login_status()
        if self.login.get('state') != 'ready':
            self._failure(task_id, 'needs_login')
            return
        try:
            official.config()  # Missing formal key/quota configuration must stop before AI creation.
            with self.store.lock:
                task = self.store.load(task_id)
                task.update(status='running', error=None)
                self.store.save(task)
            await self.page.evaluate('''() => {
              document.getElementById('meeting_bridge_file')?.remove();
              const input=document.createElement('input');input.id='meeting_bridge_file';input.type='file';input.style.display='none';document.body.append(input);
            }''')
            await self.page.locator('#meeting_bridge_file').set_input_files(str(self.store.directory(task_id) / task['original']))
            result = await self.page.evaluate(SITE_CODE, {'taskId': task_id, 'clientId': task['client_note_id']})
            if result.get('failure'):
                self._failure(task_id, result['failure'])
            else:
                await self._read_and_share(task_id)
        except official.APIError as e:
            with self.store.lock:
                task = self.store.load(task_id)
                task.update(status='configuration_required', error=str(e))
                self.store.save(task)
        except Exception:
            self._failure(task_id, 'submission_unknown')

    async def _website_read(self, task_id, note_id, expected):
        if self.login.get('state') != 'ready' or not getattr(self, 'page', None):
            return
        try:
            proof = await self.page.evaluate("""async ({id,expected}) => {
              try {
                let r;window.webpackChunkiget_biji.push([['mb_verify_'+Date.now()],{},x=>{r=x}]);
                const v=await r(72656).Z.getNoteDetail(id);
                return {business_status:v.status_code,note_id:v.data?.note_id,summary_match:v.data?.content===expected};
              } catch(e) {return {http_status:e.response?.status||null,verified:false};}
            }""", {'id':note_id, 'expected':expected})
            with self.store.lock:
                task=self.store.load(task_id)
                task['website_readback']={'at':now(),**proof}
                self.store.save(task)
            if proof.get('http_status') in (401,403):
                self.force_login_expiry=self.login.get('tokenExpireAt',0)
                self.login={'state':'needs_login'}
        except Exception:
            pass  # Formal-key recovery remains readable even if the website is offline.

    async def _read_and_share(self, task_id):
        task = self.store.load(task_id)
        try:
            note = await asyncio.to_thread(official.detail, task['note_id'])
            content = note.get('content') or ''
            sentences = (note.get('audio') or {}).get('sentences') or []
            with self.store.lock:
                task = self.store.load(task_id)
                task['readback'] = {'at': now(), 'count': task.get('readback', {}).get('count', 0)+1, 'http_success': True}
                task['restart_read_required'] = False
                if not content or not sentences:
                    task.update(status='known_processing', error='已找到原笔记但结果尚未完整；稍后只读回，不重新生成。')
                    self.store.save(task)
                    return
                summary_same = self.store.text_once(task_id, 'summary.md', content)
                transcript = '\n'.join(s['text'] for s in sentences) + '\n'
                transcript_same = self.store.text_once(task_id, 'transcript.txt', transcript)
                # Preserve the first raw output; a differing formal read is kept separately.
                if not summary_same:
                    self.store.text_once(task_id, 'formal-summary.md', content)
                if not transcript_same:
                    self.store.text_once(task_id, 'formal-transcript.txt', transcript)
                task['readback'].update(summary_exact_match=summary_same, transcript_exact_match=transcript_same)
                task['status'] = 'results_saved'
                self.store.save(task)
                sharing = task['share']
                if sharing['status'] == 'unknown':
                    task.update(status='share_unknown', error='分享结果未知，未重复发送；原始结果已保留。')
                    self.store.save(task)
                    return
                if sharing['status'] != 'success':
                    # Save an unknown intent before the one sharing write.
                    task['share'].update(status='unknown', note_id=task['note_id'], exclude_audio=True, intent_at=now())
                    self.store.save(task)
            if sharing['status'] != 'success':
                url = await asyncio.to_thread(official.share, task['note_id'], sharing['request_id'])
                svg = official.qr_bytes(url)
                p = self.store.directory(task_id) / 'qr.svg'
                p.write_bytes(svg)
                p.chmod(0o600)
                with self.store.lock:
                    task = self.store.load(task_id)
                    task['share'].update(status='success', url=url, at=now())
                    self.store.save(task)
            await self._website_read(task_id, task['note_id'], content)
            with self.store.lock:
                task = self.store.load(task_id)
                task.update(status='complete', phase='complete', error=None)
                self.store.save(task)
        except official.APIError as e:
            with self.store.lock:
                task = self.store.load(task_id)
                task.update(status='recovery_required', error=str(e))
                self.store.save(task)
        except Exception:
            self._failure(task_id, 'submission_unknown')

    def show_login(self):
        async def show():
            try:
                await self.page.bring_to_front()
                button = self.page.get_by_role('button', name='注册/登录', exact=True)
                if await button.count() == 1:
                    await button.click()
                tab = self.page.get_by_role('tab', name='App 扫码登录', exact=True)
                if await tab.count() == 1:
                    await tab.click()
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(show(), self.loop)

    def close(self):
        if not getattr(self, 'stopped', False):
            self.loop.call_soon_threadsafe(self.stop_event.set)
            # A running evaluation must stop; close only this context and mark identity unknown.
            if self.pending:
                future = asyncio.run_coroutine_threadsafe(self.context.close(), self.loop)
                try:
                    future.result(10)
                except Exception:
                    pass
            self.thread.join(15)
