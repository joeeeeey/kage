"""Bounded on-demand Slack previews: bodies live only in a short-lived memory cache."""
from __future__ import annotations
from collections import OrderedDict
import contextlib
import html
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error

from kage import locale

TTL=15


MAX_CACHED=16


MAX_MESSAGES=20


MAX_TEXT=8000


class PreviewError(Exception):
    def __init__(self,code,status=503,retry_after=15):
        super().__init__(code);self.code=code;self.status=status;self.retry_after=retry_after


def safe_error(exc):
    match=re.fullmatch(r'Slack API error: ([a-z_]+)',str(exc))
    if match:return match[1]
    if isinstance(exc,PreviewError):return exc.code
    if isinstance(exc,urllib.error.HTTPError) and exc.code==429:return 'ratelimited'
    return 'preview_unavailable'


def _secret_thread(db,channel,root):
    """P38: the thread hosts a card waiting for a verification code (its watch is secret_input): mask it like that card."""
    if not channel or not root:
        return False
    try:
        return db.execute("SELECT 1 FROM escalation_cards c JOIN escalations x ON x.event_id=c.event_id JOIN watches w ON "
                          "w.id=x.watch_id WHERE c.channel=? AND coalesce(c.thread_root,x.thread_ts)=? AND "
                          "json_extract(w.data,'$.secret_input') LIMIT 1",(channel,str(root))).fetchone() is not None
    except sqlite3.Error:
        return False


class PreviewService:
    def __init__(self,path,api=None,token=None,clock=time.time, *, core):
        self.core = core
        self.path=Path(path);self.api=api;self.token=token;self.clock=clock
        self.cache=OrderedDict();self.lock=threading.Lock();self.cooldown=0;self.auth_until=0
        self.auth_lock=threading.Lock();self.active=threading.BoundedSemaphore(2);self.inflight=set()

    def authorize(self):
        with self.auth_lock:
            if self.api is not None:return
            module=self.core.slack_transport()
            token=module.default_user_token();identity=module.api_call('auth.test',token)
            if identity.get('team_id')!=self.core.slack_workspace or identity.get('user_id')!=self.core.owner_slack_user or identity.get('bot_id'):
                raise PreviewError('unexpected_slack_identity',403)
            self.token=token;self.api=module.api_call

    def request(self,method,params):
        return self.api(method,self.token,params=params)

    def fetch(self,w):
        channel=w['channel'];root=w.get('thread');partial=False
        if not re.fullmatch(r'[CDG][A-Z0-9]+',str(channel)):
            raise PreviewError('invalid_watch_scope',400)
        if root:
            if not re.fullmatch(r'\d+\.\d+',str(root)):raise PreviewError('invalid_watch_scope',400)
            first=self.request('conversations.replies',{'channel':channel,'ts':root,'limit':1})
            source=next((m for m in first.get('messages',[]) if m.get('ts')==root),None)
            if source is None:raise PreviewError('source_not_found',404)
            latest=str(source.get('latest_reply') or root)
            if not re.fullmatch(r'\d+\.\d+',latest):latest=root
            oldest=str(max(float(root),float(latest)-86400))
            response=self.request('conversations.replies',{'channel':channel,'ts':root,'oldest':oldest,'inclusive':True,'limit':100})
            messages=response.get('messages',[])
            partial=bool(response.get('has_more')) or float(oldest)>float(root) or len(messages)>MAX_MESSAGES
            # For very busy threads, include the newest known reply explicitly and
            # mark missing intervening pages instead of unbounded pagination.
            if latest!=root and not any(m.get('ts')==latest for m in messages):
                exact=self.request('conversations.replies',{'channel':channel,'ts':root,'oldest':latest,'latest':latest,'inclusive':True,'limit':1})
                messages.extend(m for m in exact.get('messages',[]) if m.get('ts')==latest)
                partial=True
            messages=[m for m in messages if (m.get('thread_ts') or root)==root]
        else:
            response=self.request('conversations.history',{'channel':channel,'oldest':w.get('since','0'),'limit':MAX_MESSAGES})
            messages=response.get('messages',[]);partial=bool(response.get('has_more'))
        unique={str(m.get('ts')):m for m in messages if re.fullmatch(r'\d+\.\d+',str(m.get('ts','')))}
        messages=sorted(unique.values(),key=lambda m:float(m['ts']))[-MAX_MESSAGES:]
        with contextlib.closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            db.row_factory=sqlite3.Row
            rendered=[self.core.render_message(db,channel,m,bool(w.get('secret_input')) or _secret_thread(db,channel,root)) for m in messages]
        now=self.clock()
        return {'watch_id':w['id'],'channel':channel,'root':root,'scope':'thread' if root else 'dm' if channel.startswith('D') else 'channel',
                'fetched_at':now,'expires_at':now+TTL,'partial':partial,'note':self.core.msg('previews.PreviewService.fetch.note'),
                'messages':[m for m in rendered if m]}

    def preview(self,wid):
        # Resolve only exact stored watch IDs; callers cannot choose arbitrary Slack scopes.
        with contextlib.closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            row=db.execute('SELECT data FROM watches WHERE id=?',(wid,)).fetchone()
        if row is None:raise PreviewError('watch_not_found',404)
        w=json.loads(row[0]);now=self.clock();key=(wid,w.get('channel'),w.get('thread'),w.get('since'))
        with self.lock:
            for k,(expiry,value) in list(self.cache.items()):
                if expiry<=now:self.cache.pop(k,None)
            entry=self.cache.get(key)
            if entry:
                self.cache.move_to_end(key)
                if isinstance(entry[1],PreviewError):raise entry[1]
                return entry[1]
            if self.cooldown>now:raise PreviewError('ratelimited',429,max(1,int(self.cooldown-now)))
            if key in self.inflight:raise PreviewError('preview_busy',429,2)
            self.inflight.add(key)
        if not self.active.acquire(blocking=False):
            with self.lock:self.inflight.discard(key)
            raise PreviewError('preview_busy',429,2)
        try:
            self.authorize();result=self.fetch(w)
            with self.lock:
                self.cache[key]=(result['expires_at'],result)
                while len(self.cache)>MAX_CACHED:self.cache.popitem(last=False)
            self.expire_later(key,TTL)
            return result
        except Exception as exc:
            delay=TTL;status=exc.status if isinstance(exc,PreviewError) else 503
            if isinstance(exc,urllib.error.HTTPError) and exc.code==429:
                try:delay=max(TTL,int(exc.headers.get('Retry-After',60)))
                except (ValueError,TypeError):delay=60
                self.cooldown=self.clock()+delay;status=429
            code=safe_error(exc)
            if code=='ratelimited':
                status=429;self.cooldown=max(self.cooldown,self.clock()+delay)
            error=PreviewError(code,status,delay)
            with self.lock:
                self.cache[key]=(self.clock()+delay,error)
                while len(self.cache)>MAX_CACHED:self.cache.popitem(last=False)
            self.expire_later(key,delay)
            raise error from None
        finally:
            with self.lock:self.inflight.discard(key)
            self.active.release()

    def expire_later(self,key,delay):
        def clear():
            with self.lock:
                entry=self.cache.get(key)
                if entry and entry[0]<=self.clock():self.cache.pop(key,None)
        timer=threading.Timer(delay+0.1,clear);timer.daemon=True;timer.start()


class Previews:
    """The owner-bound half of the message previews: the owner's Slack user and workspace (the identity check),
    the owner's name before a profile is cached, the words in the owner's language and the deployment's
    Slack transport and code mask; the PreviewService holds one."""

    def __init__(self, language, *, owner_key, owner_name, owner_slack_user, slack_workspace, slack_transport, mask_codes):
        self.language = language
        self.owner_slack_user = owner_slack_user
        self.slack_workspace = slack_workspace
        self.slack_transport = slack_transport
        self.mask_codes = mask_codes
        self.key, self.name = owner_key, owner_name

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def profile(self, db,uid):
        row=None
        if re.fullmatch(r'U[A-Z0-9]+',str(uid)):
            try:row=db.execute('SELECT name,updated,length(avatar) AS size FROM dashboard_profiles WHERE user_id=?',(uid,)).fetchone()
            except sqlite3.OperationalError:pass
        return {'id':uid,'name':row['name'] if row else self.name if uid==self.owner_slack_user else uid or 'Slack app',
                'avatar_url':f"/api/avatars/{uid}?v={int(row['updated'])}" if row and row['size'] else None,
                'is_owner':uid==self.owner_slack_user}

    def render_message(self, db,channel,m,secret=False):
        ts=str(m.get('ts',''))
        if not re.fullmatch(r'\d+\.\d+',ts):return None
        text=html.unescape(str(m.get('text') or ''))[:MAX_TEXT]
        # A blocker card waiting for a verification code (secret_input watch): humans' text is masked even in memory.
        if secret and not m.get('bot_id'):text=self.mask_codes(text)
        # No attachment contents, thumbnails, URLs or raw blocks in the API response.
        return {'ts':ts,'author':self.profile(db,m.get('user') or m.get('bot_id') or ''),
                'text':text,
                'is_bot':bool(m.get('bot_id')),'files':[{'title':str(f.get('title') or f.get('name') or 'File')[:240],
                'mimetype':str(f.get('mimetype') or '')[:100]} for f in m.get('files',[])[:5]],
                'slack_url':f'https://app.slack.com/archives/{channel}/p{ts.replace(".","")}'}
