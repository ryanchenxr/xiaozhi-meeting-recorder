"""Only formal OpenAPI detail and audio-excluded sharing. No web login exports."""
import io
import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit
import qrcode
import qrcode.image.svg

BASE = 'https://openapi.biji.com/open/api/v1'
CONFIG = Path(__file__).resolve().parent / 'config.local.json'

class APIError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def config():
    cfg = {'openapi_config': 'openapi.local.json'}
    if CONFIG.exists():
        if CONFIG.stat().st_mode & 0o077:
            raise APIError('本地配置需为600权限')
        cfg.update(json.loads(CONFIG.read_text()))
    p = (CONFIG.parent / cfg['openapi_config']).resolve()
    if not p.exists() or p.stat().st_mode & 0o077:
        raise APIError('请配置本机OpenAPI凭证文件，权限600')
    creds = json.loads(p.read_text())
    if not all(creds.get(k) for k in ('openapi_key', 'client_id', 'existing_member_quota_confirmed')):
        raise APIError('缺少已确认会员额度的新平台凭证')
    return creds

def request(path, payload=None, request_id=None):
    cfg = config()
    headers = {'Authorization': cfg['openapi_key'], 'X-Client-ID': cfg['client_id'], 'Content-Type': 'application/json'}
    if request_id:
        headers['Idempotency-Key'] = request_id
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method='POST' if payload is not None else 'GET')
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as res:
            status = res.status
            body = json.loads(res.read(5_000_000))
    except urllib.error.HTTPError as e:
        raise APIError('正式接口HTTP ' + str(e.code)) from None
    except Exception:
        raise APIError('正式接口请求结果未知，请读回同一任务') from None
    if not 200 <= status < 300 or body.get('success') is not True:
        raise APIError('正式接口业务失败；保留请求身份')
    return body['data']

def detail(note_id):
    if not str(note_id).isdigit():
        raise APIError('笔记身份无效')
    return request('/resource/note/detail?id=' + str(note_id))['note']

def share(note_id, request_id):
    data = request('/resource/note/sharing', {'note_id': str(note_id), 'share_exclude_audio': True}, request_id)
    url = data.get('share_url', '')
    p = urlsplit(url)
    if not (p.scheme == 'https' and p.hostname in ('biji.ddmaster.com', 'www.biji.com', 'biji.com') and p.path.startswith('/note/share_note/') and not p.query and not p.fragment and not p.username and not p.password and p.port in (None, 443)):
        raise APIError('分享响应地址不符合已验证格式，保留现场')
    return url

def qr_bytes(url):
    buffer = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
    return buffer.getvalue()
