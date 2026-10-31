"""Private SQLite identity/avatar cache; Slack bodies are never persisted."""
import contextlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from kage import locale

_OWNER_DEFAULT = object()  # a parameter whose default is the owner's value, filled in at call time


USER_RE = re.compile(r'U[A-Z0-9]+\Z')


PROFILE_TTL = 7 * 86400


THREAD_TTL = 900


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS dashboard_profiles (
      user_id TEXT PRIMARY KEY,name TEXT NOT NULL,avatar BLOB,mime TEXT,
      updated REAL NOT NULL DEFAULT 0,next_refresh REAL NOT NULL DEFAULT 0,error TEXT);
    CREATE TABLE IF NOT EXISTS dashboard_channels (
      channel TEXT PRIMARY KEY,name TEXT,kind TEXT,peer TEXT,
      updated REAL NOT NULL DEFAULT 0,next_refresh REAL NOT NULL DEFAULT 0,error TEXT);
    CREATE TABLE IF NOT EXISTS dashboard_messages (
      channel TEXT NOT NULL,ts TEXT NOT NULL,root TEXT NOT NULL,author TEXT NOT NULL,
      PRIMARY KEY(channel,ts));
    CREATE INDEX IF NOT EXISTS dashboard_messages_root ON dashboard_messages(channel,root);
    CREATE TABLE IF NOT EXISTS dashboard_sync (
      key TEXT PRIMARY KEY,cursor TEXT,updated REAL NOT NULL DEFAULT 0,
      next_refresh REAL NOT NULL DEFAULT 0,partial INTEGER NOT NULL DEFAULT 1,error TEXT);
    CREATE TABLE IF NOT EXISTS dashboard_meta (key TEXT PRIMARY KEY,value TEXT);
    ''')
    revision=db.execute("SELECT value FROM dashboard_meta WHERE key='participant_schema'").fetchone()
    if not revision or revision[0]!='2':
        db.execute('UPDATE dashboard_sync SET cursor=NULL,updated=0,next_refresh=0,partial=1')
        db.execute("INSERT OR REPLACE INTO dashboard_meta VALUES ('participant_schema','2')")
    db.commit()


def seed_events(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'events', 'watches'} <= tables:
        return
    source_join = 'LEFT JOIN event_sources s ON s.event_id=e.id' if 'event_sources' in tables else ''
    source = 's.root,' if source_join else ''
    db.execute(f'''INSERT OR IGNORE INTO dashboard_messages(channel,ts,root,author)
      SELECT e.channel,e.ts,COALESCE({source}json_extract(w.data,'$.thread'),e.ts),e.author
      FROM events e JOIN watches w ON w.id=e.watch_id {source_join}
      WHERE e.author GLOB 'U*' ''')
    db.commit()


def allowed_image_url(url):
    parsed=urllib.parse.urlsplit(url)
    host=(parsed.hostname or '').lower()
    return parsed.scheme=='https' and not parsed.username and parsed.port in (None,443) and (host in ('i0.wp.com','i1.wp.com','i2.wp.com') or any(host==suffix or host.endswith('.'+suffix) for suffix in ('slack-edge.com','slack.com','slack-imgs.com','gravatar.com')))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not allowed_image_url(newurl):
            raise ValueError('avatar_redirect_host_rejected')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def error_code(exc):
    match=re.fullmatch(r'Slack API error: ([a-z_]+)',str(exc))
    if match:return match[1]
    if isinstance(exc,urllib.error.HTTPError):return f'HTTP_{exc.code}'
    if isinstance(exc,ValueError) and re.fullmatch(r'avatar_[a-z_]+|unsupported_avatar_type',str(exc)):return str(exc)
    return type(exc).__name__


class CacheWorker:
    def __init__(self,path,api=None,token=None,downloader=_OWNER_DEFAULT, *, core):
        self.core = core
        downloader = self.core.download_avatar if downloader is _OWNER_DEFAULT else downloader
        self.path=Path(path);self.api=api;self.token=token;self.downloader=downloader
        self.stop_event=threading.Event();self.thread=None;self.cooldown=0

    def connect(self):
        db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=rw',uri=True,timeout=5);db.row_factory=sqlite3.Row
        return db

    def authorize(self):
        if self.api is not None:return
        module=self.core.slack_transport()
        token=module.default_user_token();identity=module.api_call('auth.test',token)
        if identity.get('team_id')!=self.core.slack_workspace or identity.get('user_id')!=self.core.owner_slack_user or identity.get('bot_id'):
            raise ValueError('unexpected_slack_identity')
        self.api=module.api_call;self.token=token

    def call(self,method,params):
        return self.api(method,self.token,params=params)

    def sync_once(self,now=None):
        now=time.time() if now is None else now
        if now<self.cooldown:return
        self.authorize()
        with contextlib.closing(self.connect()) as db:
            seed_events(db)
            watches=[json.loads(r[0]) for r in db.execute('SELECT data FROM watches')]
            # Enrich only currently running registered scopes, never scan workspace channels.
            active=[w for w in watches if w.get('state')=='running']
            channels=sorted({w['channel'] for w in active})
            users={self.core.owner_slack_user}
            for w in active:users.update(p for p in w.get('peers',[]) if USER_RE.fullmatch(str(p)))
            priority_users=set(users)
            users.update(r[0] for r in db.execute('SELECT DISTINCT author FROM dashboard_messages'))
            for channel in channels:
                row=db.execute('SELECT next_refresh FROM dashboard_channels WHERE channel=?',(channel,)).fetchone()
                if row and row[0]>now:continue
                try:
                    c=self.call('conversations.info',{'channel':channel})['channel']
                    db.execute('INSERT OR REPLACE INTO dashboard_channels VALUES (?,?,?,?,?,?,?)',(channel,c.get('name') or channel,'dm' if c.get('is_im') else 'channel',c.get('user'),now,now+86400,None))
                    if c.get('user'):users.add(c['user'])
                except Exception as exc:
                    if isinstance(exc,urllib.error.HTTPError) and exc.code==429:raise
                    db.execute('INSERT INTO dashboard_channels(channel,name,updated,next_refresh,error) VALUES (?,?,?,?,?) ON CONFLICT(channel) DO UPDATE SET next_refresh=excluded.next_refresh,error=excluded.error',(channel,channel,0,now+3600,error_code(exc)))
                db.commit()
                break # at most one new channel metadata call per cycle
            scopes={}
            for w in active:
                if w.get('thread'):
                    key=f"{w['channel']}/{w['thread']}";scopes[key]=(w['channel'],w['thread'],w.get('since','0'))
            due=[]
            for key,scope in scopes.items():
                r=db.execute('SELECT * FROM dashboard_sync WHERE key=?',(key,)).fetchone()
                if not r or r['next_refresh']<=now:due.append((r['updated'] if r else 0,key,scope,r))
            for _,key,(channel,root,since),r in sorted(due)[:3]:
                try:
                    # Recent bounded sample on first fill; incremental subsequent observations.
                    oldest=r['cursor'] if r and r['cursor'] else str(max(float(root),now-30*86400))
                    result=self.call('conversations.replies',{'channel':channel,'ts':root,'oldest':oldest,'limit':100,'inclusive':True})
                    messages=result.get('messages',[]);self.core.observe(db,channel,messages,root)
                    current_users={m['user'] for m in messages if self.core.valid_human(m)}
                    users.update(current_users);priority_users.update(current_users)
                    high=max([oldest]+[m['ts'] for m in messages if m.get('ts')],key=float)
                    # Pagination is intentionally bounded; partial remains truthful.
                    partial=bool(result.get('has_more')) or bool(r and r['partial']) or (not r and float(oldest)>float(root))
                    db.execute('INSERT OR REPLACE INTO dashboard_sync VALUES (?,?,?,?,?,?)',(key,high,now,now+(60 if result.get('has_more') else THREAD_TTL),int(partial),None))
                except Exception as exc:
                    if isinstance(exc,urllib.error.HTTPError) and exc.code==429:raise
                    db.execute('INSERT INTO dashboard_sync(key,updated,next_refresh,partial,error) VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET next_refresh=excluded.next_refresh,error=excluded.error',(key,0,now+3600,1,error_code(exc)))
                db.commit()
            ordered=sorted(users,key=lambda u:(u!=self.core.owner_slack_user,u not in priority_users,u))
            pending=[]
            for uid in ordered:
                if not USER_RE.fullmatch(uid):continue
                row=db.execute('SELECT next_refresh FROM dashboard_profiles WHERE user_id=?',(uid,)).fetchone()
                if not row or row[0]<=now:pending.append(uid)
            for uid in pending[:8]:
                name=uid
                try:
                    u=self.call('users.info',{'user':uid})['user'];profile=u.get('profile',{})
                    name=profile.get('display_name') or profile.get('real_name') or u.get('name') or uid
                    url=profile.get('image_192') or profile.get('image_72')
                    data,mime=self.downloader(url) if url else (None,None)
                    db.execute('INSERT OR REPLACE INTO dashboard_profiles VALUES (?,?,?,?,?,?,?)',(uid,name,data,mime,now,now+PROFILE_TTL,None))
                except Exception as exc:
                    if isinstance(exc,urllib.error.HTTPError) and exc.code==429:raise
                    db.execute('INSERT INTO dashboard_profiles(user_id,name,next_refresh,error) VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=CASE WHEN excluded.name=excluded.user_id THEN dashboard_profiles.name ELSE excluded.name END,next_refresh=excluded.next_refresh,error=excluded.error',(uid,name,now+3600,error_code(exc)))
                db.commit()
            errors=[r[0] for r in db.execute('SELECT DISTINCT error FROM dashboard_profiles WHERE error IS NOT NULL UNION SELECT DISTINCT error FROM dashboard_channels WHERE error IS NOT NULL UNION SELECT DISTINCT error FROM dashboard_sync WHERE error IS NOT NULL')]
            db.execute('INSERT OR REPLACE INTO dashboard_meta VALUES (?,?)',('last_sync',str(now)))
            db.execute('INSERT OR REPLACE INTO dashboard_meta VALUES (?,?)',('error',','.join(errors)))
            db.commit()

    def run(self):
        while not self.stop_event.is_set():
            try:self.sync_once()
            except Exception as exc:
                delay=60
                if isinstance(exc,urllib.error.HTTPError) and exc.code==429:
                    try:delay=max(60,float(exc.headers.get('Retry-After',60)))
                    except (TypeError,ValueError):pass
                self.cooldown=time.time()+delay
                with contextlib.suppress(Exception),contextlib.closing(self.connect()) as db:
                    db.execute('INSERT OR REPLACE INTO dashboard_meta VALUES (?,?)',('error',error_code(exc)));db.commit()
            self.stop_event.wait(60)

    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True,name='avatar-cache');self.thread.start()


class Avatars:
    """The owner-bound half of the participant cache: the owner's Slack user and workspace (the sync worker's
    identity check), the owner's name before a profile is cached, the guard variable and the deployment's
    Slack transport; the CacheWorker holds one."""

    def __init__(self, language, *, owner_key, owner_name, owner_slack_user, slack_workspace, guard_env, slack_transport):
        self.language = language
        self.owner_slack_user = owner_slack_user
        self.slack_workspace = slack_workspace
        self.guard_env = guard_env
        self.slack_transport = slack_transport
        self.key, self.name = owner_key, owner_name

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def valid_human(self, m):
        # Owner-authored automation is activity under the owner's identity, never approval.
        return (USER_RE.fullmatch(str(m.get('user', ''))) and (not m.get('bot_id') or m.get('user') == self.owner_slack_user)
                and m.get('subtype') in (None, 'file_share', 'thread_broadcast'))

    def observe(self, db, channel, messages, default_root=None):
        """Call inside caller's transaction; one message counts once, even on re-read."""
        for m in messages:
            if not self.valid_human(m) or not re.fullmatch(r'\d+\.\d+', str(m.get('ts', ''))):
                continue
            root = m.get('thread_ts') or default_root or m['ts']
            if not re.fullmatch(r'\d+\.\d+', str(root)):
                continue
            db.execute('INSERT OR IGNORE INTO dashboard_messages VALUES (?,?,?,?)',
                       (channel, m['ts'], root, m['user']))

    def person(self, db, uid, count=0, latest=None):
        row = db.execute('SELECT name,mime,updated,error,length(avatar) AS bytes FROM dashboard_profiles WHERE user_id=?', (uid,)).fetchone()
        return {'id': uid, 'name': row['name'] if row else self.name if uid == self.owner_slack_user else uid,
                'avatar_url': f"/api/avatars/{uid}?v={int(row['updated'])}" if row and row['bytes'] else None,
                'is_owner': uid == self.owner_slack_user, 'message_count': count, 'last_interaction': latest,
                'profile_status': 'error' if row and row['error'] else 'cached' if row else 'pending',
                'observed': count > 0}

    def participants(self, db, channel, root=None, peers=()):
        sql = 'SELECT author,count(*) AS n,max(CAST(ts AS REAL)) AS latest FROM dashboard_messages WHERE channel=?'
        args = [channel]
        if root:
            sql += ' AND root=?';args.append(root)
        rows = db.execute(sql+' GROUP BY author ORDER BY latest DESC', args).fetchall()
        by_id = {row['author']: self.person(db, row['author'], row['n'], row['latest']) for row in rows}
        for uid in [self.owner_slack_user, *peers]:
            if USER_RE.fullmatch(str(uid)):
                by_id.setdefault(uid, self.person(db, uid))
        ordered = sorted(by_id.values(), key=lambda p: (not p['is_owner'], -(p['last_interaction'] or 0), p['id']))
        count = sum(p['message_count'] for p in ordered)
        return dict(participants=ordered, visible_participants=ordered[:3], participant_count=len(ordered),
                    overflow_count=max(0,len(ordered)-3), interaction_count=count,
                    activity_level=min(1, math.log1p(count)/math.log(101)))

    def enrich(self, db, state):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'dashboard_profiles' not in tables:
            return state
        profile_map = {}
        for w in state['watches']:
            channel = db.execute('SELECT * FROM dashboard_channels WHERE channel=?', (w['channel'],)).fetchone()
            peers = list(w.get('peers') or [])
            if channel and channel['peer']:
                peers.append(channel['peer'])
            root = w['thread'] if w['scope'] != 'channel' else None
            people = self.participants(db,w['channel'],root,peers)
            sync = db.execute('SELECT * FROM dashboard_sync WHERE key=?',(f"{w['channel']}/{root or 'channel'}",)).fetchone()
            activity_rows=db.execute('SELECT root,author,count(*) AS n,max(CAST(ts AS REAL)) AS latest FROM dashboard_messages WHERE channel=?'+(' AND root=?' if root else '')+' GROUP BY root,author', [w['channel'],root] if root else [w['channel']]).fetchall()
            w['participant_activity']=[{'channel':w['channel'],'root':r['root'],'author':r['author'],'count':r['n'],'last':r['latest']} for r in activity_rows]
            w.update(people, channel_name=channel['name'] if channel and channel['name'] else w['channel'],
                     conversation_type='dm' if str(w['channel']).startswith('D') else 'channel_thread' if root else 'channel',
                     participant_source='slack_metadata' if sync and sync['updated'] else 'local_events' if people['interaction_count'] else 'peer_binding',
                     participants_partial=not sync or bool(sync['partial']), participants_updated_at=sync['updated'] if sync else None)
            if channel and channel['kind']=='dm' and channel['peer']:
                w['channel_name']='DM · '+self.person(db,channel['peer'])['name']
            for p in w['participants']:
                profile_map[p['id']]=p
        watches={w['id']:w for w in state['watches']}
        for session in state['sessions']:
            ids=session['watch_ids'];relevant=[watches[x] for x in ids if x in watches]
            selected={};refs=set()
            for w in relevant:
                for p in w['participants']:
                    current=selected.setdefault(p['id'],{**p,'message_count':0,'last_interaction':None})
                    current['last_interaction']=max(current['last_interaction'] or 0,p['last_interaction'] or 0) or None
                root=w['thread'] if w['scope']!='channel' else None
                rows=db.execute('SELECT channel,ts,author FROM dashboard_messages WHERE channel=?'+(' AND root=?' if root else ''),[w['channel'],root] if root else [w['channel']])
                for channel,ts,author in rows:
                    if (channel,ts) not in refs:
                        refs.add((channel,ts))
                        if author in selected:selected[author]['message_count']+=1
            ordered=sorted(selected.values(),key=lambda p:(not p['is_owner'],-(p['last_interaction'] or 0),p['id']))
            for p in ordered:p['observed']=p['message_count']>0
            count=len(refs)
            session.update(participants=ordered,visible_participants=ordered[:3],participant_count=len(ordered),overflow_count=max(0,len(ordered)-3),interaction_count=count,activity_level=min(1,math.log1p(count)/math.log(101)))
        for event in state['events']:
            event['author_profile']=profile_map.get(event['author']) or self.person(db,event['author'])
            event['channel_name']=watches.get(event['watch_id'],{}).get('channel_name',event['channel'])
        state['owner']=self.person(db,self.owner_slack_user)
        meta={r[0]:r[1] for r in db.execute('SELECT key,value FROM dashboard_meta')}
        profiles=db.execute('SELECT count(*) FROM dashboard_profiles WHERE avatar IS NOT NULL').fetchone()[0]
        state['avatar_cache']={'status':'degraded' if meta.get('error') else 'ready' if profiles else 'empty',
                               'last_sync':float(meta['last_sync']) if meta.get('last_sync') else None,
                               'profiles':profiles,'error':meta.get('error') or None}
        return state

    def download_avatar(self, url):
        if not allowed_image_url(url):raise ValueError('avatar_host_rejected')
        if os.environ.get(self.guard_env)=='1':raise RuntimeError(f'real_slack_disabled: {self.guard_env}=1')  # P47
        with urllib.request.build_opener(SafeRedirect()).open(urllib.request.Request(url,headers={'User-Agent':'FollowupObservatory/2'}),timeout=10) as response:
            data=response.read(1024*1024+1)
        if len(data)>1024*1024:raise ValueError('avatar_too_large')
        if data.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
        elif data.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
        elif data[:6] in (b'GIF87a',b'GIF89a'):mime='image/gif'
        elif data.startswith(b'RIFF') and data[8:12]==b'WEBP':mime='image/webp'
        else:raise ValueError('unsupported_avatar_type')
        return data,mime
