"""Shared metadata-only cadence update for the poller and successful chat sends.

DMs bind requests, not people: a send as the owner into a DM extends only the SENDING task's own watches
there (its agent metadata names the task; the other person's messages and other tasks' sends never extend them), and
records the task's conversation window (`dm_windows`, `note_window`) that lets a live back-and-forth stay with it."""
from __future__ import annotations
import json
from pathlib import Path
import re
import sqlite3
import time

DELAYS = [60, 120, 120, 300, 300, 300, 600, 600, 600, 900]
VERSION = 'activity-1-2-2-5-5-5-10-10-10-15'


def clamp(value, deadline):
    return min(value, deadline) if deadline is not None else value


def activity(w, sent, now):
    """Reset cadence once per new source timestamp; never reopen a closed watch."""
    if w['state'] != 'running' or sent <= max(float(w['since']), w.get('cadence_activity_ts', w['started'])):
        return False
    if w.get('deadline') is not None and sent > w['deadline']:
        return False
    w.update(step=0, cadence_activity_ts=sent, cadence_reset_at=now, cadence_version=VERSION,
             next_poll=clamp(now+DELAYS[0], w.get('deadline')))
    return True




def dm_like(db, channel, conv_type=None):
    """A DM (D…) or a group DM intake knows (intake_convs.conv_type mpim)."""
    if conv_type in ('im', 'mpim') or str(channel).startswith('D'):
        return True
    try:
        row = db.execute('SELECT conv_type FROM intake_convs WHERE channel=?', (channel,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] == 'mpim')


def note_window(db, channel, ts, task_id, kind=None, peer=None, now=None, conv_type=None):
    """Inside the caller's transaction (no commit): `task_id` just wrote as the owner into DM / group DM `channel` — open or
    extend its conversation window (a new send re-opens a released one). False when nothing was recorded."""
    if not task_id or not re.fullmatch(r'\d+\.\d+', str(ts or '')) or not dm_like(db, channel, conv_type):
        return False
    now = time.time() if now is None else now
    try:
        db.execute('INSERT INTO dm_windows (channel,task_id,peer,last_ts,last_at,kind) VALUES (?,?,?,?,?,?) '
                   'ON CONFLICT(channel,task_id) DO UPDATE SET last_ts=CASE WHEN CAST(excluded.last_ts AS REAL)>'
                   'CAST(dm_windows.last_ts AS REAL) THEN excluded.last_ts ELSE dm_windows.last_ts END,'
                   'last_at=max(dm_windows.last_at,excluded.last_at),kind=excluded.kind,'
                   'peer=coalesce(excluded.peer,dm_windows.peer),released_at=NULL,release_reason=NULL',
                   (str(channel), str(task_id), peer, str(ts), now, kind))
    except sqlite3.OperationalError:  # a database from before P39 (no dm_windows yet)
        return False
    return True


def _kind(metadata):
    payload = (metadata or {}).get('event_payload') if isinstance(metadata, dict) else None
    return payload.get('kind') if isinstance(payload, dict) else None

class PollSchedule:
    """The steps that need the deployment: the owner and bot ids (only the owner's sends extend watches; mentions
    never count them), the task identity of a path (`task_id_for_cwd`)."""

    def __init__(self, *, owner_user, bot_user, task_id_for_cwd):
        self.owner_user, self.bot_user, self.task_id_for_cwd = owner_user, bot_user, task_id_for_cwd

    def sender_task(self, metadata):
        """The task an agent post speaks for: its hidden metadata's task_cwd (the deployment's task identity lookup)."""
        payload = (metadata or {}).get('event_payload') if isinstance(metadata, dict) else None
        if not isinstance(payload, dict) or not payload.get('task_cwd'):
            return None
        try:
            return self.task_id_for_cwd(str(payload['task_cwd']))
        except Exception:  # noqa: BLE001 - no task identity, no window
            return None

    def record_outbound(self, channel, ts, root, author, db_path, now=None, metadata=None):
        """No API, no bodies, no event delivery; update only matching existing watches. P39: in a DM only the sending
        task's own watches (`metadata` = the post's hidden agent metadata), plus its conversation window."""
        if author != self.owner_user or not re.fullmatch(r'[CDG][A-Z0-9]+', str(channel)) or not re.fullmatch(r'\d+\.\d+',str(ts)):
            return []
        path = Path(db_path).expanduser()
        if not path.exists():
            return []
        now = time.time() if now is None else now
        sent = float(ts)
        changed = []
        task = self.sender_task(metadata)
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=2)
        try:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT data FROM watches WHERE json_extract(data,'$.state')='running' AND (json_extract(data,'$.channel')=? OR json_extract(data,'$.dm')=?)",(channel,channel)).fetchall()
            for row in rows:
                w=json.loads(row[0])
                if w.get('thread') and channel==w['channel'] and str(root or ts)!=w['thread']:
                    continue
                if str(channel).startswith('D') and w.get('kind', 'peer') == 'peer' and task is not None and \
                        self.task_id_for_cwd(w.get('cwd')) != task:
                    continue  # an agent's send in a DM extends only its OWN task's watches there
                if w.get('lifecycle') == 'idle' and sent <= w['deadline'] and sent > w.get('last_activity',w['started']):
                    w.update(last_activity=sent,deadline=sent+w.get('inactivity_seconds',172800))
                if activity(w,sent,now):
                    db.execute('UPDATE watches SET data=? WHERE id=?',(json.dumps(w,ensure_ascii=False),w['id']))
                    changed.append(w['id'])
            if _kind(metadata) == 'agent_message':  # a helper send; the runtime notes its own windows, ask cards none
                note_window(db, channel, ts, task, 'helper_send', now=now)
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:
            db.close()
        return changed

    def mentions(self, users, task):
        """`agent_sends.mentions` of an agent post as the owner / bot: the people it @-mentions (never the owner or the bot) and
        the task it speaks for, as JSON (ids only, never the text); None without a task or anyone else mentioned."""
        users = [u for u in dict.fromkeys(users or ()) if re.fullmatch(r'[UW][A-Z0-9]+', str(u)) and u not in (self.owner_user, self.bot_user)]
        return json.dumps({'task': task, 'users': users}) if task and users else None

    def record_mentions(self, channel, ts, users, db_path, now=None, metadata=None):
        """The chat client's post-send hook. An agent's helper post (`agent_message`; the
        runtime's own posts record theirs in `_ledger`) that @-mentions people goes into the send ledger (kind helper_send)
        with whom it mentioned and its task, so the inbox task sees that task waiting on them (open asks). Nothing
        without a task or a mention; never raises into the send."""
        record = self.mentions(users, self.sender_task(metadata)) if _kind(metadata) == 'agent_message' else None
        path = Path(db_path).expanduser()
        if not record or not re.fullmatch(r'[CDG][A-Z0-9]+', str(channel)) or not re.fullmatch(r'\d+\.\d+', str(ts)) \
                or not path.exists():
            return False
        session = metadata['event_payload'].get('session')
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=2)
        try:
            with db:
                db.execute('INSERT OR IGNORE INTO agent_sends (channel,ts,kind,ref,session,created,mentions) VALUES '
                           '(?,?,?,?,?,?,?)', (channel, ts, 'helper_send', None, session,
                                               time.time() if now is None else now, record))
        except sqlite3.OperationalError:  # a database from before P46.3 (no mentions column)
            return False
        finally:
            db.close()
        return True
