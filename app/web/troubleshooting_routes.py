"""Troubleshooting API; actions remain governed by saved broadcast settings."""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from ..diagnostic_logs import redact
from ..troubleshooting import SOURCES, diagnostics, utc

router = APIRouter(prefix='/troubleshoot')


class ReplayRequest(BaseModel):
    source: Literal['all','bom','rfs','traffic'] = 'all'
    mode: Literal['reprocess','resend'] = 'reprocess'


class StartRequest(BaseModel):
    token: str


def manager(request):
    return request.app.state.troubleshooting


@router.get('/status')
async def status(request: Request):
    return diagnostics(request.app)


@router.post('/preview')
async def preview(request: Request, body: ReplayRequest):
    try:
        return await manager(request).preview(body.source,body.mode)
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))
    except ValueError as exc:
        raise HTTPException(400,str(exc))


@router.post('/replay')
async def replay(request: Request, body: StartRequest):
    try:
        return manager(request).start(body.token)
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))
    except ValueError as exc:
        raise HTTPException(409,str(exc))


@router.get('/job')
async def job(request: Request):
    return manager(request).job or {'status':'idle'}


@router.post('/cancel')
async def cancel(request: Request):
    service=manager(request)
    if service.job and service.task and not service.task.done():
        service.job['cancel_requested']=True
        service.save()
    return service.job or {'status':'idle'}


@router.post('/poll/{source}')
async def poll_now(request: Request, source: Literal['all','bom','rfs','traffic']):
    service=manager(request)
    if service.lock.locked() or service.task and not service.task.done():
        raise HTTPException(409,'Another troubleshooting operation is running')
    selected=SOURCES if source=='all' else (source,)
    service.job=dict(id=utc(),mode='poll',source=source,status='running',started=utc(),finished='',
                     cancel_requested=False,processed=0,total=len(selected),results={},error='')
    service.save()
    async def run():
        try:
            async with service.lock:
                for name in selected:
                    if service.job['cancel_requested']:
                        break
                    p=getattr(request.app.state,'poller' if name=='bom' else name+'_poller')
                    await p.poll_once()
                    service.job['processed']+=1
                    service.job['results'][name]=p.status.last_poll_result if name=='bom' else p.last_result
                    service.save()
            service.job['status']='cancelled' if service.job['cancel_requested'] else 'completed'
        except asyncio.CancelledError:
            service.job['status']='interrupted'
        except Exception as exc:
            service.job.update(status='failed',error=str(exc))
        finally:
            service.job['finished']=utc()
            service.save()
    service.task=asyncio.create_task(run())
    return service.job


@router.get('/logs')
async def logs(request: Request, after: int = Query(0,ge=0), service: str = '', level: str = '', search: str = ''):
    return request.app.state.process_logs.query(after,service,level,search)


@router.get('/logs/download', response_class=PlainTextResponse)
async def download_logs(request: Request, service: str = '', level: str = '', search: str = ''):
    data=request.app.state.process_logs.query(0,service,level,search)
    return PlainTextResponse('\n'.join(json.dumps(row) for row in data['rows']),
                             headers={'Content-Disposition':'attachment; filename="noticeecho-logs.jsonl"'})


@router.get('/bundle')
async def bundle(request: Request):
    data=dict(generated=utc(),diagnostics=diagnostics(request.app),
              settings=redact(request.app.state.db.all_settings()),
              logs=request.app.state.process_logs.query(),job=manager(request).job)
    return Response(json.dumps(redact(data),indent=2),media_type='application/json',
                    headers={'Content-Disposition':'attachment; filename="noticeecho-diagnostics.json"'})
