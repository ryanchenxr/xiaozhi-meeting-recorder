"""Bounded metadata only. Durations use the receiver's monotonic clock, not device time."""
from collections import OrderedDict, deque
import time
import json
import threading
from store import atomic

MAX_VALUE=(1<<63)-1
FIELDS=('body_us','chunk_us','pcm_write_us','pcm_fsync_us','state_save_us','reply_us')
class RequestTimings:
    MAX_SESSIONS=2
    MAX_RECORDS=32
    def __init__(self, root, clock=time.monotonic_ns):
        self.root,self.clock=root,clock
        self.sessions=OrderedDict()
        self.lock=threading.RLock();self.save_lock=threading.Lock()
        self.evicted_sessions=0
        self.lost_records=0
    def begin(self,sid,seq,attempt):
        try:
            return dict(session=sid,seq=seq,attempt=attempt,begin_ns=self.clock(),http=0,reply_ok=False,duplicate=False,connection_id=0,connection_request=0,**{k:0 for k in FIELDS})
        except Exception:
            self.lost_records=min(MAX_VALUE,self.lost_records+1);return None
    def now(self):
        try:return self.clock()
        except Exception:
            self.lost_records=min(MAX_VALUE,self.lost_records+1);return 0
    def elapsed(self,row,field,started):
        try:
            if row is not None:row[field]=min(MAX_VALUE,max(0,self.clock()-started)//1000)
        except Exception:self.lost_records=min(MAX_VALUE,self.lost_records+1)
    def field(self,row,key,value):
        try:
            if row is not None:row[key]=value
        except Exception:self.lost_records=min(MAX_VALUE,self.lost_records+1)
    def complete(self,row):
        with self.lock:self._complete(row)
    def _complete(self,row):
        if row is None:return
        try:
            sid=row['session']
            row['total_us']=min(MAX_VALUE,max(0,self.clock()-row['begin_ns'])//1000)
            row['begin_us']=min(MAX_VALUE,row.pop('begin_ns')//1000)
            if sid not in self.sessions:
                if len(self.sessions)==self.MAX_SESSIONS:
                    self.sessions.popitem(last=False);self.evicted_sessions=min(MAX_VALUE,self.evicted_sessions+1)
                self.sessions[sid]=dict(rows=deque(maxlen=self.MAX_RECORDS),total=0,slow_success=0,totals={k:0 for k in FIELDS},save_failed=False)
            s=self.sessions[sid];self.sessions.move_to_end(sid)
            s['rows'].append(dict(row));s['total']=min(MAX_VALUE,s['total']+1)
            if row['http']==200 and row['reply_ok'] and row['total_us']>=100000:s['slow_success']=min(MAX_VALUE,s['slow_success']+1)
            for k in FIELDS:s['totals'][k]=min(MAX_VALUE,s['totals'][k]+row[k])
        except Exception:self.lost_records=min(MAX_VALUE,self.lost_records+1)
    def flush(self,sid=None):
        # Serialize diagnostic saves, but never hold the RAM lock during disk I/O.
        with self.save_lock:
            try:
                with self.lock:
                    entries=[]
                    for key,s in self.sessions.items():
                        if sid is not None and key!=sid:continue
                        payload=dict(v=1,session=key,clock='receiver_monotonic_us',total=s['total'],slow_success=s['slow_success'],totals=dict(s['totals']),records=[dict(r) for r in s['rows']],dropped=max(0,s['total']-len(s['rows'])),evicted_sessions=self.evicted_sessions,previous_save_failed=s['save_failed'],lost_records=self.lost_records)
                        entries.append((key,s,payload))
            except Exception:
                self.lost_records=min(MAX_VALUE,self.lost_records+1);return
            for key,s,payload in entries:
                try:
                    if not (self.root/key/'state.json').exists():continue
                    if len(json.dumps(payload,indent=2).encode())>32768:raise ValueError('timing size cap')
                    atomic(self.root/key/'request-timings.json',payload)
                    with self.lock:s['save_failed']=False
                except Exception:
                    with self.lock:s['save_failed']=True
