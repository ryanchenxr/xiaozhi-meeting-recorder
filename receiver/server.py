"""Loopback-only receiver. Browser automation never navigates to this server."""
import fcntl
import html
from email.parser import BytesParser
from email.policy import default as email_policy
import json
import os
import secrets
import signal
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qs
from store import Store, GateError, MAX_BYTES, EXTENSIONS, atomic, quota_text
from browser import BrowserWorker

ROOT = Path(__file__).resolve().parent
PORT = 8766
TOKEN = secrets.token_urlsafe(32)

class App:
    def __init__(self, private=None, worker_factory=BrowserWorker):
        self.store = Store(private or ROOT / 'private')
        self.worker = worker_factory(self.store)

    def receive_audio(self, path, name=None):
        task, created = self.store.receive_file(path, name)
        # Duplicate admission never creates a second task. Recovery is note-id based.
        if task['status'] not in ('complete', 'unknown', 'share_unknown'):
            self.worker.enqueue(task['id'])
        return self.store.public(self.store.load(task['id'])), created

    def status(self):
        with self.store.lock:
            budget = json.loads(self.store.budget.read_text())
            return {'login': self.worker.login, 'tasks': [self.store.public(t) for t in self.store.tasks()], 'limit': budget.get('limit'), 'used': len(budget['submissions'])}

class Server(ThreadingHTTPServer):
    daemon_threads = True

def rendered_page(app, notice=''):
    source=(ROOT/'web/index.html').read_text()
    state=app.status()
    cards=[]
    for task in state['tasks']:
        esc=lambda v:html.escape(str(v),quote=True)
        tid=task['id']
        card='<article><h3>'+esc(task['name'])+'</h3><p>'+esc(task['status'])+' · '+esc(task['phase'])+'</p><small>note_id '+esc(task.get('note_id') or '尚未取得')+'</small>'
        if task.get('error'):card+='<p class="error">'+esc(task['error'])+'</p>'
        card+='<audio controls preload="none" src="/api/tasks/'+tid+'/audio"></audio>'
        card+='<h4>原始转写</h4><pre>'+esc(task['transcript'] or '尚未取得')+'</pre><h4>得到原始摘要</h4><pre>'+esc(task['summary'] or '尚未取得')+'</pre>'
        if task.get('share',{}).get('status')=='success':
            card+='<h4>本任务结果分享（排除音频）</h4><a target="_blank" rel="noreferrer noopener" href="'+esc(task['share']['url'])+'">打开本任务分享页</a><img width="240" src="/api/tasks/'+tid+'/qr.svg" alt="本任务分享二维码">'
        card+='</article>';cards.append(card)
    source=source.replace('{{CSRF}}',TOKEN)
    source=source.replace('<div id="tasks"></div>','<div id="tasks">'+(''.join(cards) or '<p>尚无任务</p>')+'</div>')
    source=source.replace('正在连接本机程序…','已登录（项目专用会话）' if state['login'].get('state')=='ready' else '需要在专用官网登录 / 等待浏览器启动')
    quota=quota_text(state["used"],state["limit"])
    source=source.replace('<span id="quota"></span>',f'<span id="quota"> {quota}</span>')
    message={'existing':f'已找到同一文件的原任务，未重复生成；{quota}。','received':'音频已接收，程序正在自动处理；结果会自动更新，也可刷新查看。'}.get(notice,'')
    source=source.replace('{{NOTICE}}',message or '选择音频后，原始转写、摘要与本任务二维码显示在这里。')
    source=source.replace('<p id="message" role="status"></p>','<p id="message" role="status">'+message+'</p>')
    return source.encode()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _host(self):
        return self.headers.get('Host') in ('127.0.0.1:' + str(PORT), 'localhost:' + str(PORT))

    def send(self, status, data, mime='application/json', attachment=None):
        body = json.dumps(data, ensure_ascii=False).encode() if mime == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') or mime=='application/json' else ''))
        self.send_header('Content-Length', str(len(body)))
        if attachment:
            self.send_header('Content-Disposition', 'attachment; filename="' + attachment + '"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; media-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._host():
            self.send(403, {'error': '仅允许本机访问'})
            return
        path = urlsplit(self.path).path
        try:
            if path == '/sample.wav':
                self.send(200, (ROOT / 'sample.wav').read_bytes(), 'audio/wav', attachment='meeting-bridge-synthetic.wav')
            elif path == '/':
                notice=parse_qs(urlsplit(self.path).query).get('notice',[''])[0]
                self.send(200,rendered_page(self.server.app,notice),'text/html')
            elif path in ('/app.js', '/style.css'):
                f, mime = {'/': ('index.html','text/html'), '/app.js': ('app.js','text/javascript'), '/style.css': ('style.css','text/css')}[path]
                self.send(200, (ROOT / 'web' / f).read_bytes(), mime)
            elif path == '/api/status':
                self.send(200, {**self.server.app.status(), 'csrf': TOKEN})
            elif path.startswith('/api/tasks/') and path.endswith('/qr.svg'):
                task_id = path.split('/')[3]
                task = self.server.app.store.load(task_id)
                if task['share']['status'] != 'success':
                    self.send(404, {'error': '本任务尚无有效分享链接'})
                    return
                self.send(200, (self.server.app.store.directory(task_id) / 'qr.svg').read_bytes(), 'image/svg+xml')
            elif path.startswith('/api/tasks/') and path.endswith('/audio'):
                task = self.server.app.store.load(path.split('/')[3])
                p = self.server.app.store.directory(task['id']) / task['original']
                mime = {'.wav':'audio/wav','.m4a':'audio/mp4','.mp3':'audio/mpeg','.aac':'audio/aac','.flac':'audio/flac'}[p.suffix]
                self.send(200, p.read_bytes(), mime)
            else:
                self.send(404, {'error': '不存在'})
        except (GateError, FileNotFoundError):
            self.send(404, {'error': '任务或结果不存在'})

    def do_POST(self):
        if urlsplit(self.path).path in ('/receive-form','/sample-form'):
            self.native_form()
            return
        origin = self.headers.get('Origin')
        if not self._host() or (origin and origin not in ('http://127.0.0.1:'+str(PORT), 'http://localhost:'+str(PORT))) or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), TOKEN):
            self.send(403, {'error': '本机请求校验失败'})
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/tasks':
                size = int(self.headers.get('Content-Length', '0'))
                name = Path(unquote(self.headers.get('X-File-Name', ''))).name
                ext = Path(name).suffix.lower()
                if not 0 < size <= MAX_BYTES or ext not in EXTENSIONS:
                    self.send(400, {'error': '音频格式或大小不符合要求（64MiB以内）'})
                    self.close_connection = True
                    return
                fd, file_path = tempfile.mkstemp(dir=self.server.app.store.root, suffix=ext)
                try:
                    with os.fdopen(fd,'wb') as f:
                        remaining=size
                        while remaining:
                            block=self.rfile.read(min(1024*1024, remaining))
                            if not block:
                                raise GateError('音频接收中断，请保留原文件')
                            f.write(block)
                            remaining -= len(block)
                    task, created = self.server.app.receive_audio(file_path, name)
                    self.send(202 if created else 200, {'task': task, 'created': created})
                finally:
                    Path(file_path).unlink(missing_ok=True)
            elif path.startswith('/api/tasks/') and path.endswith('/start'):
                task_id = path.split('/')[3]
                task = self.server.app.store.load(task_id)
                if not task.get('note_id') and (task.get('submit_started') or task.get('remote_started')):
                    self.send(409, {'error': '远端结果未知且没有note_id；禁止重新创建', 'task':self.server.app.store.public(task)})
                    return
                self.server.app.worker.enqueue(task_id)
                self.send(202, {'task':self.server.app.store.public(task)})
            elif path == '/api/login':
                self.server.app.worker.show_login()
                self.send(200, {'ok': True})
            elif path == '/api/stop':
                self.send(200, {'ok':True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send(404, {'error':'不存在'})
        except (GateError, ValueError) as e:
            self.send(409, {'error': str(e)})
        except Exception:
            self.send(500, {'error': '本地操作失败；保留任务，请查看原任务状态，不要重传'})

    def native_form(self):
        origin=self.headers.get('Origin')
        if not self._host() or (origin and origin not in ('http://127.0.0.1:'+str(PORT),'http://localhost:'+str(PORT))):
            atomic(self.server.app.store.root/'http-rejection.json',{'at':__import__('store').now(),'host_matches':self._host(),'origin_class':'null' if origin=='null' else 'other','route':'native_form'})
            self.send(403,{'error':'本机请求校验失败，请刷新本机页面后再试'});return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=MAX_BYTES+65536:raise GateError('表单大小不符合要求')
            body=self.rfile.read(size)
            if len(body)!=size:raise GateError('文件接收中断')
            path=urlsplit(self.path).path
            if path=='/sample-form':
                fields=parse_qs(body.decode())
                if not secrets.compare_digest(fields.get('csrf',[''])[0],TOKEN):
                    self.send(403,{'error':'表单校验失败，请刷新页面'});return
                task,created=self.server.app.receive_audio(ROOT/'sample.wav','合成接口测试.wav')
            else:
                content_type=self.headers.get('Content-Type','')
                if not content_type.startswith('multipart/form-data;'):raise GateError('文件表单格式无效')
                message=BytesParser(policy=email_policy).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
                if not message.is_multipart():raise GateError('文件表单格式无效')
                parts=list(message.iter_parts())
                csrf=next((x.get_payload(decode=True).decode() for x in parts if x.get_param('name',header='content-disposition')=='csrf'),'')
                if not secrets.compare_digest(csrf,TOKEN):
                    self.send(403,{'error':'表单校验失败，请刷新页面'});return
                files=[x for x in parts if x.get_param('name',header='content-disposition')=='audio']
                if len(files)!=1:raise GateError('请选择一份音频')
                name=Path(files[0].get_filename() or '').name
                ext=Path(name).suffix.lower();data=files[0].get_payload(decode=True)
                if ext not in EXTENSIONS or not 0<len(data)<=MAX_BYTES:raise GateError('音频格式或大小无效')
                fd,target=tempfile.mkstemp(dir=self.server.app.store.root,suffix=ext)
                try:
                    with os.fdopen(fd,'wb') as f:f.write(data)
                    task,created=self.server.app.receive_audio(target,name)
                finally:Path(target).unlink(missing_ok=True)
            self.send_response(303);self.send_header('Location','/?notice='+('received' if created else 'existing')+'#results');self.send_header('Content-Length','0');self.send_header('Cache-Control','no-store');self.end_headers()
        except (GateError,ValueError,UnicodeError):
            self.send(409,{'error':'表单无法接收，请选择有效音频或查看已有任务；不会重复创建'})


def main():
    os.umask(0o077)
    private=ROOT/'private'
    private.mkdir(mode=0o700,exist_ok=True)
    lock=open(private/'process.lock','a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('会议接收原型已运行，请打开 http://127.0.0.1:8766/')
    server=Server(('127.0.0.1', PORT),Handler)
    app=App(private)
    server.app=app
    from device_receiver import configured_server
    try:
        device_server=configured_server(app)
    except Exception:
        app.worker.close()
        server.server_close()
        raise SystemExit("设备接收入口无法启动；检查受限配置、局域网连接及端口占用。")
    if device_server:
        threading.Thread(target=device_server.serve_forever,daemon=True).start()
        if device_server.discovery:
            try: device_server.discovery.start()
            except OSError: print('设备自动发现端口不可用；设备将改用固件内的备用地址。',flush=True)
    atomic(private/'control.json',{'pid':os.getpid(),'port':PORT,'csrf':TOKEN})
    def stop(*_):
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,stop)
    signal.signal(signal.SIGTERM,stop)
    print('会议接收原型：http://127.0.0.1:8766/\n专用官网浏览器用于登录与处理；首次请正常扫码登录。\n停止：Ctrl+C 或运行 stop.command。',flush=True)
    try:
        server.serve_forever()
    finally:
        if device_server:
            if device_server.discovery: device_server.discovery.stop()
            device_server.shutdown()
            device_server.server_close()
            device_server.receiver.timings.flush()
        server.server_close()
        app.worker.close()
        (private/'control.json').unlink(missing_ok=True)
        fcntl.flock(lock,fcntl.LOCK_UN)
        lock.close()

if __name__=='__main__':
    main()
