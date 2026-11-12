"""Packaged desktop service: one process, local durable queue, no external services."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--frontend', required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    os.environ['APP_DATA_DIR'] = str(Path(args.data_dir).resolve())
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    # Credentials arrive through an inherited environment, never CLI arguments.
    configured = bool(os.environ.get('DEEPSEEK_API_KEY', '').strip())
    os.environ.setdefault('DEEPSEEK_API_KEY', 'desktop-unconfigured')
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    os.chdir(args.data_dir)

    import uvicorn
    from fastapi import Depends, Request, HTTPException
    from api.app import create_app, mount_react_frontend
    from api.auth import require_api_key
    from api.local_queue import LocalBroker, LocalDailyBroker, LocalResearchBroker
    from api.redis_runs import (RedisChatRunManager, RedisChatRunWorker,
        RedisDailyRunManager, RedisDailyRunWorker, RedisResearchRunManager, RedisResearchRunWorker)
    from api.run_dispatcher import RunDispatcher, worker_process_lock
    from api_worker import enqueue_enabled_daily_run
    from config import Config
    from daily_orchestrator import DailyResearchOrchestrator
    from research_agent import ResearchAgent
    from scheduler import Scheduler

    cfg = Config.load({'data_dir': args.data_dir})
    with worker_process_lock(cfg.runtime_paths.database_dir / 'api-worker.lock'):
        agent = ResearchAgent(cfg=cfg)
        queue = cfg.runtime_paths.database_dir / 'desktop-queue.db'
        chat, research, daily = LocalBroker(queue), LocalResearchBroker(queue), LocalDailyBroker(queue)
        for broker in (chat, research, daily):
            broker.recover_abandoned()
        scheduler = Scheduler(cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds)
        daily_manager = RedisDailyRunManager(scheduler, daily)
        orchestrator = DailyResearchOrchestrator(
            scheduler=scheduler, llm_client=agent.llm_client, model=agent.model,
            request_timeout_seconds=cfg.daily_request_timeout_seconds,
            max_keyword_concurrency=cfg.daily_keyword_concurrency,
            max_results_per_keyword=cfg.daily_max_results_per_keyword,
            daily_sources=cfg.daily_sources, openalex_api_key=cfg.openalex_api_key, ieee_api_key=cfg.ieee_api_key)
        dispatcher = RunDispatcher({
            'chat': (RedisChatRunWorker(agent, chat), 2),
            'research': (RedisResearchRunWorker(agent, research), 2),
            'daily': (RedisDailyRunWorker(orchestrator, scheduler, daily, paper_store=agent.paper_store), 1),
        })
        stop = threading.Event()
        worker = threading.Thread(target=dispatcher.run_forever, args=(stop,), name='desktop-worker', daemon=True)
        app = create_app(agent=agent, chat_run_manager=RedisChatRunManager(agent, chat),
                         research_run_manager=RedisResearchRunManager(agent, research),
                         daily_run_manager=daily_manager,
                         api_key=os.environ['API_AUTH_TOKEN'], require_api_key=True)

        @asynccontextmanager
        async def lifespan(_app):
            if configured:
                worker.start()
                enqueue_enabled_daily_run(cfg, daily_manager)
                agent.start_rag_warmup()
            yield
            stop.set()
            with dispatcher._lock:
                for entry in dispatcher._entries.values():
                    entry.cancel.set()
            if worker.is_alive():
                await __import__('asyncio').to_thread(worker.join, 20)

        app.router.lifespan_context = lifespan

        @app.middleware('http')
        async def local_only(request: Request, call_next):
            from fastapi.responses import JSONResponse
            host = request.headers.get('host', '').split(':')[0]
            if host not in {'127.0.0.1', 'localhost'}:
                return JSONResponse({'detail': 'invalid host'}, status_code=403)
            if not configured and request.method == 'POST' and any(
                s in request.url.path for s in ('/runs', '/chat', '/research-projects', '/translate')
            ):
                return JSONResponse({'detail': '请先在设置中配置模型 API Key'}, status_code=409)
            return await call_next(request)

        @app.get('/api/v1/desktop/status', dependencies=[Depends(require_api_key)])
        def desktop_status():
            return {'configured': configured, 'data_dir': str(cfg.runtime_paths.root),
                    'worker_alive': worker.is_alive(), 'queued': sum(b.queue_depth() for b in (chat, research, daily))}

        @app.post('/api/v1/desktop/shutdown', dependencies=[Depends(require_api_key)])
        def shutdown():
            server.should_exit = True
            return {'stopping': True}

        if not mount_react_frontend(app, args.frontend):
            raise RuntimeError('前端构建文件缺失，请重新安装客户端')
        sock = socket.socket()
        sock.bind(('127.0.0.1', args.port))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='warning', timeout_graceful_shutdown=5))
        print('DESKTOP_READY=' + json.dumps({'port': port}), flush=True)
        server.run(sockets=[sock])
        sock.close()


if __name__ == '__main__':
    __import__('multiprocessing').freeze_support()
    main()
