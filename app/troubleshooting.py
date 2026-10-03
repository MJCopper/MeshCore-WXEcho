"""Source diagnostics and serialized, previewed replay jobs."""
from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import time

from . import __version__
from .config import polling_seconds, QUEUE_MAX, QUEUE_BYTE_MAX
from .db import Database
from .bom_enricher import BOMEnrichment, WarningSection
from .diagnostic_logs import redact
from .filters import FilterRules
from .poller import BomPoller
from .rfs.feed import Incident
from .rfs.poller import RFSPoller
from .traffic.feed import TrafficItem, TYPES
from .traffic.poller import TrafficPoller
from .traffic.schedule import ClosurePeriod
from .presentation import freshness

SOURCES = ('bom', 'rfs', 'traffic')
TABLES = {'bom': ('bom_current', "region='NSW'", 'alert_id'),
          'rfs': ('rfs_incidents', 'missing_polls<2', 'incident_id'),
          'traffic': ('traffic_items', 'missing_polls<2 AND active=1', 'item_id')}


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def rows(db, sql, parameters=()):
    with db._lock:
        return [dict(row) for row in db._conn.execute(sql, parameters).fetchall()]


def current(db, source):
    table, where, _ = TABLES[source]
    return rows(db, f'SELECT * FROM {table} WHERE {where}')


def fingerprint(db, sources):
    values = {source: current(db, source) for source in sources}
    # Include dedupe/baseline state: a preview must not outlive another completed delivery.
    values['settings'] = db.all_settings()
    values['history'] = rows(db, 'SELECT MAX(id) AS id FROM service_history')
    values['state'] = rows(db, 'SELECT * FROM alert_state ORDER BY alert_id')
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class ReplayTx:
    supports_notice_guards = True
    def __init__(self, real, job=None):
        self.real, self.job = real, job
        self.message_budget = real.message_budget
    def enqueue_notice(self, parts, on_result=None, priority=1, valid_if=None):
        def valid():
            return (not self.job or not self.job['cancel_requested']) and (not valid_if or valid_if())
        return self.real.enqueue_notice(parts, on_result=on_result, priority=priority, valid_if=valid)
    def enqueue_verification(self, *args, **kwargs):
        return self.real.enqueue_verification(*args, **kwargs)


async def replay_source(db, tx, source, snapshot, force, state):
    """Use the same source pipelines without fetching or refreshing observation age."""
    if source == 'bom':
        poller = BomPoller(db, tx)
        live = getattr(state, 'poller', None)
        if live:
            poller._enricher = live._enricher
            poller._council_index = live._council_index
        settings = db.all_settings()
        rules = FilterRules.from_settings(settings)
        for row in snapshot:
            item = json.loads(row.get('raw_data') or '{}')
            if not item:
                raise ValueError('BOM provider input is unavailable; poll BOM before re-processing')
            if not settings.get('bom_enabled', True):
                continue
            enrichment = item.get('_enrichment')
            if enrichment:
                data = dict(enrichment)
                data['sections'] = tuple(WarningSection(**section) for section in data.get('sections',[]))
                for key in ('area_names','polygons','lga_names'):
                    data[key] = tuple(data.get(key,()))
                data['geocodes'] = tuple(tuple(code) for code in data.get('geocodes',()))
                class StoredEnrichment:
                    async def enrich(self, url):
                        return BOMEnrichment(**data)
                poller._enricher = StoredEnrichment()
            elif live:
                poller._enricher = live._enricher
            await poller._process(item, rules, settings.get('display_timezone', 'Australia/Sydney'),
                                  0, bool(settings.get('dry_run', True)), settings=settings, force=force)
            # Update selection/detail without refreshing the provider observation timestamp.
            with db._lock:
                db._conn.execute('UPDATE bom_current SET raw_data=?, selection=?, selection_reason=?, council_match=?, '
                                 'matched_councils=?, match_method=?, match_reason=? WHERE alert_id=?',
                                 (json.dumps(item),item.get('selection',''),item.get('selection_reason',''),item.get('council_match','unknown'),
                                  json.dumps(item.get('matched_councils',[])),item.get('match_method',''),
                                  item.get('match_reason',''),row['alert_id']))
                db._conn.commit()
    else:
        cls = Incident if source == 'rfs' else TrafficItem
        items = []
        for row in snapshot:
            data = json.loads(row.get('normalized_data') or '{}')
            if not data:
                raise ValueError(f'{source.upper()} provider input is unavailable; poll before re-processing')
            data = {f.name: data[f.name] for f in fields(cls) if f.name in data}
            if source == 'traffic':
                data['periods'] = tuple(ClosurePeriod(**p) for p in data.get('periods', []))
            items.append(cls(**data))
        poller = RFSPoller(db, tx) if source == 'rfs' else TrafficPoller(db, tx)
        if source == 'traffic':
            live = getattr(state, 'traffic_poller', None)
            poller._councils = getattr(live, '_councils', None)
        await poller.poll_once(replay_items=items, force=force)


class Troubleshooting:
    def __init__(self, app):
        self.app = app
        self.lock = asyncio.Lock()
        self.previews = {}
        self.task = None
        self.job = None
        db = app.state.db
        with db._lock:
            db._conn.execute('CREATE TABLE IF NOT EXISTS troubleshoot_jobs '
                             '(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            saved = db._conn.execute('SELECT data FROM troubleshoot_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
            db._conn.commit()
        if saved:
            self.job = json.loads(saved['data'])
            if self.job['status'] in ('running','queued','preparing'):
                self.job.update(status='interrupted', finished=utc(),
                                error='Application restarted; inspect History before requesting another replay')
                self.save()

    def save(self):
        db = self.app.state.db
        with db._lock:
            db._conn.execute('INSERT OR REPLACE INTO troubleshoot_jobs(id,data) VALUES(?,?)',
                             (self.job['id'], json.dumps(self.job)))
            db._conn.execute('DELETE FROM troubleshoot_jobs WHERE rowid NOT IN '
                             '(SELECT rowid FROM troubleshoot_jobs ORDER BY rowid DESC LIMIT 20)')
            db._conn.commit()

    async def preview(self, source, mode):
        if self.lock.locked() or self.task and not self.task.done():
            raise RuntimeError('Another troubleshooting operation is running')
        sources = list(SOURCES) if source == 'all' else [source]
        if any(s not in SOURCES for s in sources) or mode not in ('reprocess','resend'):
            raise ValueError('Invalid source or action')
        async with self.lock:
            original = self.app.state.db
            mark = fingerprint(original, sources)
            clone = Database(':memory:')
            with original._lock:
                original._conn.backup(clone._conn)
            snapshots = {s: current(clone, s) for s in sources}
            clone.set_setting('dry_run', True)
            details, exclusions, errors = [], [], []
            try:
                for s in sources:
                    if not clone.get_setting(s+'_enabled', s == 'bom'):
                        exclusions.append(dict(source=s,reason='Service disabled',count=len(snapshots[s])))
                        continue
                    start = rows(clone, 'SELECT COALESCE(MAX(id),0) AS id FROM service_history')[0]['id']
                    # A resend-style dry-run exposes all eligible notices without changing real state.
                    await replay_source(clone, ReplayTx(self.app.state.tx), s, snapshots[s], True, self.app.state)
                    snapshots[s] = current(clone,s)
                    records = rows(clone, 'SELECT h.* FROM service_history h JOIN '
                                   '(SELECT external_id,MAX(id) AS id FROM service_history WHERE source=? GROUP BY external_id) latest '
                                   'ON h.id=latest.id', (s,))
                    current_ids={row[TABLES[s][2]] for row in snapshots[s]}
                    records=[r for r in records if r['external_id'] in current_ids]
                    for r in records:
                        if r['transmit_status'] == 'dry-run' and r['id'] > start:
                            details.append(dict(source=s,id=r['external_id'],title=r['title'],
                                                parts=len(r['transmitted_text'].split(' || ')),text=r['transmitted_text']))
                        elif r['disposition'].startswith(('excluded','filtered','formatting-blocked')):
                            exclusions.append(dict(source=s,reason=r['detail'],count=1))
                    # Already-queued notices remain excluded even from forced preview.
                    queued = sum(r['transmit_status']=='queued' for r in records)
                    if queued:
                        exclusions.append(dict(source=s,reason='Already queued; duplicate admission suppressed',count=queued))
            except Exception as exc:
                errors.append(str(exc))
            finally:
                clone.close()
            self.previews = {key:value for key,value in self.previews.items() if value['expires']>time.time()}
            while len(self.previews) >= 5:
                self.previews.pop(next(iter(self.previews)))
            token = secrets.token_urlsafe(24)
            preview = dict(token=token,source=source,sources=sources,mode=mode,
                           dry_run=original.get_setting('dry_run',True),notices=len(details),
                           parts=sum(d['parts'] for d in details),details=details,exclusions=exclusions,
                           errors=errors,expires=time.time()+300,fingerprint=mark,snapshots=snapshots)
            self.previews[token] = preview
            return {k:v for k,v in preview.items() if k not in ('snapshots','fingerprint','expires')}

    def start(self, token):
        if self.lock.locked() or self.task and not self.task.done():
            raise RuntimeError('A troubleshooting job is already running')
        preview = self.previews.get(token)
        if not preview or preview['expires'] < time.time():
            raise ValueError('Preview expired; create a new preview')
        if preview['errors']:
            raise ValueError('Resolve preview errors before starting')
        if fingerprint(self.app.state.db, preview['sources']) != preview['fingerprint']:
            raise ValueError('Settings, notices or delivery state changed; refresh the preview')
        self.previews.pop(token)
        self.job = dict(id=secrets.token_hex(8),status='running',mode=preview['mode'],source=preview['source'],
                        started=utc(),finished='',cancel_requested=False,processed=0,total=preview['notices'],
                        results={},error='',history_ids=[],history_start=rows(self.app.state.db,'SELECT COALESCE(MAX(id),0) AS id FROM service_history')[0]['id'])
        self.save()
        self.task = asyncio.create_task(self.run(preview))
        return self.job

    async def run(self, preview):
        db = self.app.state.db
        try:
            async with self.lock:
                tx = ReplayTx(self.app.state.tx, self.job)
                async def process(source, snapshot):
                    candidate_ids={r[TABLES[source][2]] for r in snapshot}
                    live = getattr(self.app.state, "poller" if source=="bom" else source+"_poller")
                    async with live._poll_lock:
                        def latest_records():
                            return {r['external_id']:r for r in rows(db,'SELECT h.* FROM service_history h JOIN '
                                    '(SELECT external_id,MAX(id) AS id FROM service_history WHERE source=? GROUP BY external_id) latest ON h.id=latest.id',(source,))
                                    if r['external_id'] in candidate_ids}
                        before=latest_records()
                        await replay_source(db,tx,source,snapshot,preview['mode']=='resend',self.app.state)
                        for key, record in latest_records().items():
                            if before.get(key)!=record and record['id'] not in self.job['history_ids']:
                                self.job['history_ids'].append(record['id'])
                for source in preview['sources']:
                    if self.job['cancel_requested']:
                        break
                    await process(source, preview['snapshots'][source])
                # Track only replay-eligible IDs, not concurrent service traffic.
                ids = {(d['source'],d['id']) for d in preview['details']}
                while True:
                    recent = rows(db,'SELECT * FROM service_history WHERE id>=?', (min(self.job['history_ids']) if self.job['history_ids'] else self.job['history_start']+1,))
                    recent = [r for r in recent if r['id'] in self.job['history_ids']]
                    latest = {(r['source'],r['external_id']):r for r in recent if (r['source'],r['external_id']) in ids}
                    counts = {}
                    for r in latest.values():
                        status=r['transmit_status'] or 'excluded'
                        counts[status]=counts.get(status,0)+1
                    counts['unchanged'] = len(ids-latest.keys())
                    self.job.update(results=counts,processed=sum(v for k,v in counts.items() if k not in ('queued','deferred')))
                    self.save()
                    if self.job['cancel_requested'] or not any(r['transmit_status'] in ('queued','deferred') for r in latest.values()):
                        break
                    # Retry capacity deferrals only, never an uncertain/failed radio send.
                    for source in preview['sources']:
                        pending = {key[1] for key,r in latest.items() if key[0]==source and r['transmit_status']=='deferred'}
                        subset = [r for r in preview['snapshots'][source] if r[TABLES[source][2]] in pending]
                        if subset and not self.job['cancel_requested']:
                            await process(source,subset)
                    await asyncio.sleep(2)
            self.job['status']='cancelled' if self.job['cancel_requested'] else 'completed'
        except asyncio.CancelledError:
            self.job['status']='interrupted'
        except Exception as exc:
            self.job.update(status='failed',error=str(exc))
            db.add_error('troubleshoot',str(exc))
        finally:
            self.job['finished']=utc()
            self.save()

    async def close(self):
        if self.task and not self.task.done():
            self.job['cancel_requested']=True
            self.task.cancel()
            await self.task


def diagnostics(app):
    db, tx = app.state.db, app.state.tx
    settings = db.all_settings()
    services=[]
    for source in SOURCES:
        poller = getattr(app.state, 'poller' if source=='bom' else source+'_poller',None)
        status = getattr(poller,'status',poller)
        attempt = getattr(status,'last_poll_time',None) if source=='bom' else getattr(status,'last_poll','')
        success = getattr(status,'last_poll_success_time',None) if source=='bom' else getattr(status,'last_successful_poll','')
        if not success:
            success=db.get_setting(source+'_last_successful_poll','')
        if source=='bom' and not success:
            snapshots=rows(db,"SELECT fetched_at FROM bom_feed_snapshots WHERE region='NSW'")
            success=snapshots[0]['fetched_at'] if snapshots else ''
        interval=polling_seconds(settings.get(source+'_poll_minutes',5 if source=='bom' else 10),5 if source=='bom' else 10)
        enabled=settings.get(source+'_enabled',source=='bom')
        next_poll='Disabled' if not enabled else 'Due now / on startup'
        if attempt and enabled:
            next_poll=getattr(poller,"next_poll_at",None) or (datetime.fromisoformat(attempt)+timedelta(seconds=interval)).isoformat()
        history=rows(db,'SELECT disposition,transmit_status,COUNT(*) AS count FROM service_history WHERE source=? GROUP BY disposition,transmit_status',(source,))
        counts={}
        for r in history:
            label=r['transmit_status'] or r['disposition'] or 'processed'
            counts[label]=counts.get(label,0)+r['count']
        saved=current(db,source)
        latest_records=rows(db,'SELECT h.* FROM service_history h JOIN (SELECT external_id,MAX(id) AS id FROM service_history WHERE source=? GROUP BY external_id) latest ON h.id=latest.id',(source,))
        ids={row[TABLES[source][2]] for row in saved}
        last_decisions={}
        for record in latest_records:
            if record['external_id'] in ids:
                label=('excluded' if record['disposition'].startswith(('excluded','filtered')) else 'included')
                last_decisions[label]=last_decisions.get(label,0)+1
                if record['transmit_status']:
                    label=record['transmit_status']
                    last_decisions[label]=last_decisions.get(label,0)+1
        services.append(dict(source=source,enabled=enabled,interval_seconds=interval,last_attempt=attempt,
                             last_success=success,freshness=freshness(success,interval/60,enabled),next_poll=next_poll,result=getattr(status,'last_poll_result','') if source=='bom' else getattr(status,'last_result',''),
                             collected=len(saved),last_decisions=last_decisions,history_counts=counts,poll_duration_seconds=getattr(poller,"last_poll_duration",None),uncertain=sum(r.get('council_match')=='unknown' or 'fallback' in r.get('normalized_data','') for r in saved),
                             enrichment={name:sum(r.get('enrichment_status')==name for r in saved) for name in ('available','unavailable','not requested')} if source=='bom' else {},
                             exclusions=rows(db,'SELECT detail,COUNT(*) AS count FROM service_history WHERE source=? AND (disposition LIKE ? OR disposition=?) GROUP BY detail ORDER BY count DESC LIMIT 10',(source,'excluded%','filtered'))))
    queue=getattr(tx,'_queue',[])
    pending=[q for q in queue if not getattr(q,'verification',False)]
    queue_info=dict(notices=len(pending),parts=tx.queue_depth,bytes=sum(getattr(q,'remaining_bytes',len(q.text.encode())) for q in queue),
                    max_notices=QUEUE_MAX,max_bytes=QUEUE_BYTE_MAX,oldest_age_seconds=max([time.time()-getattr(q,'queued_at',time.time()) for q in queue] or [0]),
                    budget=tx.message_budget,active=bool(getattr(tx,'_active_notice',None)),last_error=tx.last_error)
    feed_status={r['feed']:dict(r) for r in db.traffic_feed_status()}
    feeds=[]
    for name in TYPES:
        record=feed_status.get(name,dict(feed=name,last_checked='',last_success='',published='',error=''))
        record['requested']=bool(settings.get('traffic_enabled') and name in settings.get('traffic_types',[]) and not (name=='fire' and settings.get('rfs_enabled')))
        record['freshness']=freshness(record['last_success'],settings.get('traffic_poll_minutes',10),record['requested'])
        feeds.append(record)
    path=Path(db.path)
    with db._lock:
        db._conn.execute('SAVEPOINT diagnostic_write_probe')
        try:
            db._conn.execute('CREATE TABLE IF NOT EXISTS diagnostic_write_probe (value INTEGER)')
            db._conn.execute('INSERT INTO diagnostic_write_probe VALUES(1)')
            writable=True
        except Exception:
            writable=False
        finally:
            db._conn.execute('ROLLBACK TO diagnostic_write_probe')
            db._conn.execute('RELEASE diagnostic_write_probe')
    return redact(dict(version=__version__,started=getattr(app.state,'started_at',''),uptime_seconds=int(time.time()-getattr(app.state,'started_epoch',time.time())),
                       restart_count=db.get_setting('diagnostic_restart_count',0),previous_start=db.get_setting('diagnostic_previous_start',''),dry_run=settings.get('dry_run',True),
                       database=dict(path=db.path,bytes=path.stat().st_size if path.is_file() else 0,writable=writable,
                                     migrations=rows(db,'SELECT name FROM history_migrations'),sqlite_version=rows(db,'SELECT sqlite_version() AS version')[0]['version']),
                       services=services,traffic_feeds=feeds,queue=queue_info,radios=tx.status(),
                       recent_errors=[dict(r) for r in db.recent_errors(20)],
                       last_confirmed=rows(db,"SELECT ts,transport FROM transmit_log WHERE success=1 ORDER BY id DESC LIMIT 1"),
                       boundaries='NSW Spatial Services snapshot dated 1 October 2026; simplified point matching does not establish a road footprint'))
