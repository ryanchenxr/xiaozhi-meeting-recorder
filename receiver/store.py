"""Durable task identities. No browser credentials or signed URLs belong here."""
import hashlib
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
EXTENSIONS = {'.wav', '.m4a', '.mp3', '.aac', '.flac'}

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def limit_reached(budget):
    """limit None = no local count cap (2026-10-20, Ryan). The submissions list stays:
    it is the durable intent record that prevents duplicate or replayed submissions."""
    limit = budget.get('limit')
    return limit is not None and len(budget['submissions']) >= limit

def quota_text(used, limit):
    return f'累计音频处理 {used}/{limit}' if limit is not None else f'累计音频处理 {used} 次（不限次数）'

def read(path, default):
    return json.loads(path.read_text()) if path.exists() else default

class GateError(Exception):
    pass

class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.lock = threading.RLock()
        self.budget = self.root / 'budget.json'
        if not self.budget.exists():
            atomic(self.budget, {'limit': None, 'submissions': []})  # r6: no local count cap by default
        self.reconcile_restart()

    def directory(self, task_id):
        if not isinstance(task_id, str) or len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
            raise GateError('任务不存在')
        return self.root / 'tasks' / task_id

    def load(self, task_id):
        p = self.directory(task_id) / 'state.json'
        if not p.exists():
            raise GateError('任务不存在')
        return read(p, {})

    def save(self, task):
        task['updated_at'] = now()
        atomic(self.directory(task['id']) / 'state.json', task)

    def tasks(self):
        return sorted([read(p, {}) for p in (self.root / 'tasks').glob('*/state.json')], key=lambda t: t['created_at'], reverse=True)

    def reconcile_restart(self):
        """A saved intent is a possible write, never an invitation to replay."""
        with self.lock:
            budget = read(self.budget, {})
            for task in self.tasks():
                submitted = any(x['task_id'] == task['id'] for x in budget['submissions'])
                task['submit_started'] = task.get('submit_started', False) or submitted
                if task.get('note_id'):
                    task['restart_read_required'] = True
                    self.save(task)
                if task.get('status') in ('running', 'received', 'known_processing'):
                    if task.get('note_id'):
                        task['status'] = 'recovery_required'
                    elif task.get('submit_started') or task.get('remote_started'):
                        task['status'] = 'unknown'
                    else:
                        task['status'] = 'interrupted_local'
                    self.save(task)
                elif submitted and not task.get('note_id') and task.get('status') not in ('unknown', 'rejected'):
                    task['status'] = 'unknown'
                    self.save(task)

    def receive_file(self, path, display_name=None):
        """Core path entry; HTTP accepts file bytes, never an arbitrary local path."""
        path = Path(path)
        ext = path.suffix.lower()
        if ext not in EXTENSIONS or not 0 < path.stat().st_size <= MAX_BYTES:
            raise GateError('请选择64MiB以内的WAV、M4A、MP3、AAC或FLAC音频')
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.lock:
            tasks = self.tasks()
            for task in tasks:
                if task['sha256'] == sha:
                    return task, False
            if any(t['status'] not in ('complete', 'rejected') for t in tasks):
                raise GateError('已有任务待处理或结果未知，请先查看原任务')
            budget = read(self.budget, {})
            if limit_reached(budget):
                raise GateError(f'累计音频处理额度已用完（{len(budget["submissions"])}/{budget["limit"]}）；相同文件仍可查看原结果')
            task_id = uuid.uuid4().hex
            directory = self.directory(task_id)
            directory.mkdir(mode=0o700, parents=True)
            target = directory / ('original' + ext)
            shutil.copyfile(path, target)
            os.chmod(target, 0o600)
            task = {'id': task_id, 'client_note_id': 'meeting-bridge-' + str(uuid.uuid4()), 'created_at': now(), 'name': Path(display_name or path.name).name[:160], 'original': target.name, 'sha256': sha, 'bytes': target.stat().st_size, 'status': 'received', 'phase': 'received', 'remote_started': False, 'submit_started': False, 'note_id': None, 'file_id': None, 'share': {'status': 'not_started', 'request_id': str(uuid.uuid4())}, 'events': []}
            self.save(task)
            return task, True

    def reserve_submission(self, task_id):
        with self.lock:
            task = self.load(task_id)
            budget = read(self.budget, {})
            if task.get('submit_started') or any(x['task_id'] == task_id for x in budget['submissions']):
                raise GateError('创建已提交或结果未知，禁止重复创建')
            if limit_reached(budget):
                raise GateError(f'累计音频处理额度已用完（{len(budget["submissions"])}/{budget["limit"]}）')
            # The budget identity is durable before changing task state or sending.
            budget['submissions'].append({'task_id': task_id, 'client_note_id': task['client_note_id'], 'intent_at': now()})
            atomic(self.budget, budget)
            task['submit_started'] = True
            self.save(task)

    def text_once(self, task_id, name, text):
        p = self.directory(task_id) / name
        if not p.exists():
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        return p.read_text() == text

    def public(self, task):
        directory = self.directory(task['id'])
        return {**{k: task.get(k) for k in ('id', 'name', 'sha256', 'bytes', 'status', 'phase', 'note_id', 'file_id', 'created_at', 'updated_at', 'error', 'upload', 'timings', 'share', 'restart_read_required', 'readback', 'website_readback')}, 'events': task.get('events', []), 'transcript': (directory / 'transcript.txt').read_text() if (directory / 'transcript.txt').exists() else '', 'summary': (directory / 'summary.md').read_text() if (directory / 'summary.md').exists() else ''}
