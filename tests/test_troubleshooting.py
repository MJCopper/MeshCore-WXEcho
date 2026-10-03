"""Operator diagnostics and recovery use isolated state and radio doubles."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from app.bom_enricher import BOMEnrichment
from app.db import Database
from app.diagnostic_logs import ProcessLogs, StreamCapture, redact
from app.poller import BomPoller
from app.rfs.feed import Incident
from app.rfs.poller import RFSPoller
from app.traffic.feed import TrafficItem
from app.traffic.poller import TrafficPoller
from app.troubleshooting import Troubleshooting, diagnostics, rows
from app.web.troubleshooting_routes import router


class Radio:
    message_budget = 126
    supports_notice_guards = True
    queue_depth = 0
    last_error = ''
    def __init__(self):
        self.sent = []
        self.defer = 0
        self.pause = False
        self.pending = []
    def status(self):
        return [dict(name='meshcore',label='MeshCore',enabled=True,connected=False,channel=0,error='',target='')]
    def enqueue_notice(self, parts, on_result=None, priority=1, valid_if=None):
        if self.defer:
            self.defer-=1
            return False
        self.sent.append(parts)
        if self.pause:
            self.pending.append((parts,on_result,valid_if))
        else:
            for index,_ in enumerate(parts):
                on_result(index,True,'')
        return True
    def enqueue_verification(self,*args,**kwargs):
        return True


class Enricher:
    async def enrich(self,url):
        return BOMEnrichment(status='available')


@pytest.fixture
def environment():
    app=FastAPI()
    db=Database(':memory:')
    radio=Radio()
    app.state.db=db
    app.state.tx=radio
    app.state.poller=BomPoller(db,radio)
    app.state.poller._enricher=Enricher()
    app.state.rfs_poller=RFSPoller(db,radio)
    app.state.traffic_poller=TrafficPoller(db,radio)
    app.state.traffic_poller._councils=[]
    app.state.process_logs=ProcessLogs()
    app.state.troubleshooting=Troubleshooting(app)
    app.include_router(router)
    for source in ('bom','rfs','traffic'):
        db.set_setting(source+'_enabled',True)
        db.set_setting(source+'_all_councils',True)
    db.set_setting('rfs_levels',['Advice'])
    db.set_setting('traffic_types',['incident'])
    db.set_setting('traffic_baseline_done',True)
    db.set_setting('dry_run',False)
    yield app
    db.close()


def seed(app,source,index=1):
    db=app.state.db
    if source=='rfs':
        item=Incident(str(index),'Grass Fire','Advice','Central Coast','Example Road','Being controlled','Grass Fire','','')
        db.rfs_save_incident(item,item.revision)
        db.rfs_mark_sent(item.incident_id,item.revision)
    elif source=='traffic':
        item=TrafficItem(f'incident:{index}','incident','CRASH','Crash','Example Road','Gosford','Central Coast',
                         'Northbound','Road closed','Avoid the area',None,None,None,None,False,None)
        db.traffic_save_item(item,'Central Coast',True)
        db.traffic_mark_sent(item.item_id,item.revision)
    else:
        item=dict(id=str(index),event='Severe Thunderstorm Warning',region='NSW',area_desc='Hunter',
                  effective='',expires=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                  message_type='Alert',headline='Storm',references=['https://www.bom.gov.au/warning'],detail='Heavy rain',
                  _enrichment=dict(status='available'))
        db.replace_bom_current([item],{'NSW'},'2026-10-03T00:00:00+00:00')
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['bom','rfs','traffic'])
async def test_preview_is_isolated_and_resend_queues_complete_eligible_notice(environment,source):
    app=environment
    seed(app,source)
    db=app.state.db
    original=db.all_settings()
    history=rows(db,'SELECT * FROM service_history')
    preview=await app.state.troubleshooting.preview(source,'resend')
    assert not preview['errors']
    assert preview['notices']==1 and preview['parts']>=1
    assert rows(db,'SELECT * FROM service_history')==history
    assert db.all_settings()==original
    assert not app.state.tx.sent
    job=app.state.troubleshooting.start(preview['token'])
    await app.state.troubleshooting.task
    assert job['status']=='completed'
    assert job['results']['success']==1
    assert len(app.state.tx.sent)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['rfs','traffic'])
async def test_reprocess_preserves_confirmed_delivery_and_observation_age(environment,source):
    app=environment
    seed(app,source)
    table='rfs_incidents' if source=='rfs' else 'traffic_items'
    before=rows(app.state.db,f'SELECT * FROM {table}')
    preview=await app.state.troubleshooting.preview(source,'reprocess')
    app.state.troubleshooting.start(preview['token'])
    await app.state.troubleshooting.task
    assert not app.state.tx.sent
    assert rows(app.state.db,f'SELECT * FROM {table}')==before
    assert app.state.troubleshooting.job['results']['unchanged']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['bom','rfs','traffic'])
async def test_resend_respects_dry_run(environment,source):
    app=environment
    seed(app,source)
    app.state.db.set_setting('dry_run',True)
    preview=await app.state.troubleshooting.preview(source,'resend')
    app.state.troubleshooting.start(preview['token'])
    await app.state.troubleshooting.task
    assert not app.state.tx.sent
    assert app.state.troubleshooting.job['results']['dry-run']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['bom','rfs','traffic'])
async def test_resend_does_not_override_disabled_services(environment,source):
    app=environment
    seed(app,source)
    app.state.db.set_setting(source+'_enabled',False)
    preview=await app.state.troubleshooting.preview(source,'resend')
    assert preview['notices']==0
    assert preview['exclusions'][0]['reason']=='Service disabled'
    app.state.troubleshooting.start(preview['token'])
    await app.state.troubleshooting.task
    assert not app.state.tx.sent


@pytest.mark.asyncio
async def test_preview_reports_exclusion_and_keeps_expiry_filter(environment):
    app=environment
    item=seed(app,'bom')
    item['expires']='2020-01-01T00:00:00+00:00'
    app.state.db.replace_bom_current([item],{'NSW'},'2026-10-03T00:00:00+00:00')
    preview=await app.state.troubleshooting.preview('bom','resend')
    assert preview['notices']==0
    assert 'expired' in preview['exclusions'][0]['reason']


@pytest.mark.asyncio
async def test_preview_is_invalidated_by_changed_settings(environment):
    app=environment
    seed(app,'rfs')
    preview=await app.state.troubleshooting.preview('rfs','resend')
    app.state.db.set_setting('dry_run',True)
    with pytest.raises(ValueError,match='changed'):
        app.state.troubleshooting.start(preview['token'])


@pytest.mark.asyncio
async def test_preview_missing_input_blocks_execution(environment):
    app=environment
    seed(app,'rfs')
    app.state.db._conn.execute("UPDATE rfs_incidents SET normalized_data='{}'")
    app.state.db._conn.commit()
    preview=await app.state.troubleshooting.preview('rfs','resend')
    assert preview['errors']
    with pytest.raises(ValueError,match='errors'):
        app.state.troubleshooting.start(preview['token'])


@pytest.mark.asyncio
async def test_preview_token_expires_and_is_single_use(environment):
    app=environment
    seed(app,'rfs')
    service=app.state.troubleshooting
    preview=await service.preview('rfs','resend')
    service.previews[preview['token']]['expires']=0
    with pytest.raises(ValueError,match='expired'):
        service.start(preview['token'])
    preview=await service.preview('rfs','resend')
    service.start(preview['token'])
    await service.task
    with pytest.raises(ValueError,match='expired'):
        service.start(preview['token'])


@pytest.mark.asyncio
async def test_deferred_forced_notice_is_retried_without_resending_confirmed_notice(environment):
    app=environment
    seed(app,'rfs',1)
    seed(app,'rfs',2)
    app.state.tx.defer=1
    preview=await app.state.troubleshooting.preview('rfs','resend')
    app.state.troubleshooting.start(preview['token'])
    await asyncio.wait_for(app.state.troubleshooting.task,5)
    assert len(app.state.tx.sent)==2
    assert app.state.troubleshooting.job['results']['success']==2


@pytest.mark.asyncio
async def test_already_queued_notice_is_not_admitted_again(environment):
    app=environment
    item=seed(app,'rfs')
    app.state.db.rfs_add_history(item,'pending','queued')
    preview=await app.state.troubleshooting.preview('rfs','resend')
    assert preview['notices']==0
    assert any('queued' in e['reason'] for e in preview['exclusions'])
    app.state.troubleshooting.start(preview['token'])
    await app.state.troubleshooting.task
    assert not app.state.tx.sent


@pytest.mark.asyncio
async def test_job_does_not_hold_source_lock_while_waiting_for_radio(environment):
    app=environment
    seed(app,'rfs')
    app.state.tx.pause=True
    preview=await app.state.troubleshooting.preview('rfs','resend')
    app.state.troubleshooting.start(preview['token'])
    await asyncio.sleep(.02)
    assert not app.state.rfs_poller._poll_lock.locked()
    with pytest.raises(RuntimeError,match='running'):
        await app.state.troubleshooting.preview('rfs','resend')
    app.state.troubleshooting.job['cancel_requested']=True
    assert not app.state.tx.pending[0][2]()
    await asyncio.wait_for(app.state.troubleshooting.task,4)
    assert app.state.troubleshooting.job['status']=='cancelled'


@pytest.mark.asyncio
async def test_interrupted_job_is_reported_after_restart(environment):
    app=environment
    seed(app,'rfs')
    app.state.tx.pause=True
    preview=await app.state.troubleshooting.preview('rfs','resend')
    app.state.troubleshooting.start(preview['token'])
    await asyncio.sleep(.01)
    await app.state.troubleshooting.close()
    restored=Troubleshooting(app)
    assert restored.job['status']=='interrupted'


@pytest.mark.asyncio
async def test_all_service_preview_and_diagnostics_routes(environment):
    app=environment
    for s in ('bom','rfs','traffic'):
        seed(app,s)
    transport=httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
        response=await client.post('/troubleshoot/preview',json={'source':'all','mode':'resend'})
        assert response.status_code==200
        assert response.json()['notices']==3
        status=await client.get('/troubleshoot/status')
        assert status.status_code==200
        assert status.json()['database']['writable']
        assert len(status.json()['services'])==3
        assert (await client.post('/troubleshoot/preview',json={'source':'bad'})).status_code==422
        assert (await client.post('/troubleshoot/replay',json={'token':'bad'})).status_code==409


@pytest.mark.asyncio
async def test_diagnostic_bundle_redacts_secrets_and_downloads_logs(environment):
    app=environment
    app.state.db.set_setting('channel_key','feeddeadbeef')
    app.state.process_logs.add('stdout','INFO','password=hunter2')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/troubleshoot/bundle')
        assert response.status_code==200
        assert 'attachment' in response.headers['content-disposition']
        assert 'feeddeadbeef' not in response.text
        assert 'hunter2' not in response.text
        log=await client.get('/troubleshoot/logs/download')
        assert '[redacted]' in log.text
        filtered=await client.get('/troubleshoot/logs?service=stderr')
        assert filtered.json()['rows']==[]


def test_logs_are_bounded_and_cursor_reports_missing_records():
    logs=ProcessLogs(capacity=2)
    for i in range(4):
        logs.add('stdout','INFO',str(i))
    assert len(logs.query()['rows'])==2
    assert logs.query(after=1)['truncated']
    assert logs.query(search='3')['rows'][0]['message']=='3'


def test_stdout_capture_handles_partial_lines_and_flush():
    logs=ProcessLogs()
    stream=StreamCapture(io.StringIO(),logs,'stdout')
    stream.write('part')
    assert not logs.query()['rows']
    stream.write('ial\n')
    stream.write('tail')
    stream.flush()
    assert [r['message'] for r in logs.query()['rows']]==['partial','tail']


def test_logs_persist_and_rotation_is_bounded(tmp_path):
    path=tmp_path/'logs.jsonl'
    logs=ProcessLogs(path)
    logs.add('stderr','ERROR','token=hidden')
    logs.close()
    restored=ProcessLogs(path)
    assert restored.query()['rows'][0]['message']=='token=[redacted]'
    assert restored.file.maxBytes==1024*1024 and restored.file.backupCount==2
    restored.close()


@pytest.mark.parametrize('value',['password=hidden','Bearer hidden','https://user:pass@example.org/path','api_key: hidden'])
def test_secret_redaction(value):
    result=redact(value)
    assert 'hidden' not in result and 'user:pass' not in result


@pytest.mark.asyncio
async def test_troubleshoot_page_renders_all_diagnostic_controls(environment):
    from app.web.routes import router as web_router
    app=environment
    app.include_router(web_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/troubleshoot')
        assert response.status_code==200
        for text in ('Service diagnostics','Re-process or resend','Live logs','stdout and stderr','diagnostic bundle'):
            assert text in response.text
        assert 'Start reviewed action' in response.text
        assert 'innerHTML=' not in response.text


@pytest.mark.asyncio
async def test_preview_keeps_existing_exclusion_reasons(environment):
    app=environment
    item=seed(app,'rfs')
    app.state.db.set_setting('rfs_levels',['Emergency Warning'])
    app.state.db.rfs_add_history(item,'','','Excluded by alert level selection','excluded-alert-level')
    preview=await app.state.troubleshooting.preview('rfs','resend')
    assert preview['notices']==0
    assert any('alert level' in e['reason'] for e in preview['exclusions'])


@pytest.mark.asyncio
async def test_replay_does_not_reset_feed_baselines_or_missing_counters(environment):
    app=environment
    seed(app,'traffic')
    app.state.db.set_setting('traffic_baseline_feeds',['incident'])
    before=app.state.db.get_setting('traffic_baseline_feeds')
    original=rows(app.state.db,'SELECT last_seen,missing_polls FROM traffic_items')
    preview=await app.state.troubleshooting.preview('traffic','resend')
    app.state.troubleshooting.start(preview['token'])
    await app.state.troubleshooting.task
    assert app.state.db.get_setting('traffic_baseline_feeds')==before
    assert rows(app.state.db,'SELECT last_seen,missing_polls FROM traffic_items')==original
    assert not app.state.db.traffic_feed_status()


@pytest.mark.asyncio
async def test_reprocess_tracks_a_reused_deferred_history_row(environment):
    from app.rfs.poller import format_incident
    app=environment
    item=seed(app,'rfs')
    app.state.db._conn.execute("UPDATE rfs_incidents SET last_sent_hash=''")
    app.state.db._conn.commit()
    text=' || '.join(format_incident(item,126))
    row_id=app.state.db.rfs_add_history(item,text,'deferred')
    app.state.tx.pause=True
    preview=await app.state.troubleshooting.preview('rfs','reprocess')
    app.state.troubleshooting.start(preview['token'])
    await asyncio.sleep(.02)
    assert row_id in app.state.troubleshooting.job['history_ids']
    assert app.state.troubleshooting.job['results']['queued']==1
    parts,callback,valid=app.state.tx.pending[0]
    assert valid()
    for index,_ in enumerate(parts):
        callback(index,True,'')
    await asyncio.wait_for(app.state.troubleshooting.task,4)
    assert app.state.troubleshooting.job['results']['success']==1


@pytest.mark.asyncio
async def test_poll_now_and_cancel_api(environment):
    app=environment
    called=[]
    async def poll():
        called.append('bom')
        app.state.poller.status.last_poll_result='ok: mocked collection'
    app.state.poller.poll_once=poll
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/troubleshoot/poll/bom')).status_code==200
        await app.state.troubleshooting.task
        assert called==['bom']
        assert (await client.get('/troubleshoot/job')).json()['status']=='completed'
        assert (await client.post('/troubleshoot/cancel')).status_code==200


def test_installed_capture_collects_logging_stdout_and_stderr():
    import sys
    logs=ProcessLogs()
    logs.install()
    try:
        print('stdout captured')
        sys.stderr.write('stderr captured\n')
        logging.getLogger('wx_echo.rfs').warning('password=secret-value')
        data=logs.query()['rows']
        assert any(r['service']=='stdout' and r['message']=='stdout captured' for r in data)
        assert any(r['service']=='stderr' and r['message']=='stderr captured' for r in data)
        assert any(r['service']=='wx_echo.rfs' and '[redacted]' in r['message'] for r in data)
    finally:
        logs.close()


@pytest.mark.asyncio
async def test_resend_job_explicitly_reports_simulation(environment):
    app=environment
    seed(app,'rfs')
    app.state.db.set_setting('dry_run',True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        preview=(await client.post('/troubleshoot/preview',json={'source':'rfs','mode':'resend'})).json()
        assert preview['notices']==1 and preview['dry_run'] is True
        started=(await client.post('/troubleshoot/replay',json={'token':preview['token']})).json()
        assert started['dry_run'] is True
        await app.state.troubleshooting.task
        result=(await client.get('/troubleshoot/job')).json()
        assert result['dry_run'] is True and result['results']['dry-run']==1
        assert app.state.tx.sent==[]
        assert app.state.db.get_setting('dry_run') is True


@pytest.mark.asyncio
async def test_resend_page_explains_why_dry_run_does_not_transmit(environment):
    from app.web.routes import router as web_router
    app=environment
    app.state.db.set_setting('dry_run',True)
    app.include_router(web_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        page=(await client.get('/troubleshoot')).text
        assert 'Dry Run is enabled: resend only simulates delivery' in page
        assert 'href="/settings/general"' in page
        assert 'Start simulation — no transmission' in page
