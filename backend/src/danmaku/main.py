import asyncio
from contextlib import asynccontextmanager, suppress
from functools import partial
import os
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import pysubs2

from danmaku.adapters.bilibili import BilibiliSource
from danmaku.adapters.bilibili_connection import probe_bilibili
from danmaku.adapters.bilibili_login import BilibiliPassport
from danmaku.adapters.bilibili_publication import BilibiliPublication
from danmaku.adapters.bilibili_sender import BilibiliGateway, BilibiliSender
from danmaku.adapters.credentials import MemoryCredentialStore
from danmaku.adapters.llm import ChatCompletions, SyntheticModel
from danmaku.adapters.subtitle_parser import parse
from danmaku.adapters.synthetic import SyntheticSource, synthetic_download_batch, synthetic_part
from danmaku.adapters.windows_credentials import WindowsCredentialStore
from danmaku.application.bilibili_login import BilibiliLogin
from danmaku.application.classification.model import ModelProvider
from danmaku.application.classification.service import Analyze
from danmaku.application.classification.settings import Settings
from danmaku.application.download.ports import DownloadSource
from danmaku.application.download.service import Download
from danmaku.application.subtitles.service import Subtitles
from danmaku.api.analysis import router as analysis_router
from danmaku.api.bilibili_login import router as bilibili_login_router
from danmaku.api.publication import router as publication_router
from danmaku.api.review import router as review_router
from danmaku.api.sending import router as sending_router
from danmaku.api.settings import router as settings_router
from danmaku.application.review.service import ReviewService
from danmaku.application.sending.connection import ConnectionProbe, SendConnection
from danmaku.application.sending.publication import Preload
from danmaku.application.sending.service import Sender, execute_send
from danmaku.application.sending.settings import BilibiliSettings
from danmaku.application.settings import WorkbenchSettings
from danmaku.api.inputs import router as inputs_router
from danmaku.api.health import router as health_router
from danmaku.api.workbench import router as workbench_router
from danmaku.application.projects import Projects
from danmaku.application.ports import CredentialStore
from danmaku.application.sessions import LOCAL_OWNER_ID, LocalSessions
from danmaku.application.tasks.service import BatchWork, Tasks
from danmaku.application.tasks.worker import BatchExecutor, Worker
from danmaku.domain.sending import SendWork
from danmaku.domain.workbench import ClassifyCreate, WorkbenchError
from danmaku.persistence.database import Database


def create_app(
    *,
    data_dir: Path | None = None,
    sessions: LocalSessions | None = None,
    execute_batch: BatchExecutor | None = None,
    live_source: DownloadSource | None = None,
    model_providers: dict[str, ModelProvider] | None = None,
    credential_store: CredentialStore | None = None,
    live_sender: Sender | None = None,
    bilibili_transport: httpx.AsyncBaseTransport | None = None,
    static_dir: Path | None = None,
    poll_interval: float = 0.2,
    connection_probe: ConnectionProbe | None = None,
) -> FastAPI:
    root = data_dir if data_dir is not None else Path(os.environ.get("DANMAKU_DATA_DIR", "data"))
    sessions = sessions if sessions is not None else LocalSessions()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        root.mkdir(parents=True, exist_ok=True)
        def publication_diagnostic(**fields):
            entry = {"time": datetime.now(timezone.utc).isoformat(), **fields}
            try:
                with (root / "publication-diagnostics.jsonl").open("a", encoding="utf-8") as output:
                    output.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass
        database = Database(root / "workbench.sqlite3")
        try:
            await run_in_threadpool(database.initialize)
            app.state.projects = Projects(database)
            ass_parser_version = f"1/pysubs2-{pysubs2.__version__}"
            app.state.subtitles = Subtitles(database, parse, {
                "ass": ass_parser_version, "txt": ass_parser_version, "srt": f"2/pysubs2-{pysubs2.__version__}",
            })
            credentials = credential_store
            if credentials is None:
                credentials = WindowsCredentialStore() if sys.platform == "win32" else MemoryCredentialStore()
            app.state.review = ReviewService(database)
            app.state.llm_settings = Settings(database, credentials)
            bilibili_credentials = credential_store
            if bilibili_credentials is None:
                bilibili_credentials = (
                    WindowsCredentialStore("Bilibili") if sys.platform == "win32" else MemoryCredentialStore()
                )
            async with (
                httpx.AsyncClient(follow_redirects=False, trust_env=False) as client,
                httpx.AsyncClient(follow_redirects=False, trust_env=False) as probe_client,
                httpx.AsyncClient(
                    follow_redirects=False, trust_env=False, transport=bilibili_transport, timeout=8,
                ) as platform_client,
            ):
                probe = connection_probe
                if probe is None and live_sender is None and bilibili_transport is None:
                    probe = partial(probe_bilibili, probe_client)
                app.state.send_connection = connection = SendConnection(probe)
                gateway = BilibiliGateway(platform_client, connection)
                bilibili_settings = BilibiliSettings(database, bilibili_credentials, gateway.profile)
                tasks = Tasks(database, mock_part=synthetic_part,
                              has_saved_credential=bilibili_settings.has_saved_credential)
                await run_in_threadpool(tasks.recover)
                app.state.tasks = tasks
                app.state.bilibili_settings = bilibili_settings
                app.state.bilibili_login = login = BilibiliLogin(
                    bilibili_settings, BilibiliPassport(platform_client), gateway.profile,
                )
                app.state.preload = preload = Preload(
                    database, app.state.projects, tasks.sending, login,
                    BilibiliPublication(platform_client, publication_diagnostic), diagnostic=publication_diagnostic,
                )
                await run_in_threadpool(preload.recover, LOCAL_OWNER_ID)
                app.state.settings = WorkbenchSettings(database, bilibili_settings, app.state.llm_settings)

                def live_fetch_source(owner_id: str) -> DownloadSource:
                    return BilibiliSource(bilibili_settings.download_credential(owner_id))

                download = Download(
                    database, SyntheticSource(), live_source if live_source is not None else BilibiliSource(),
                    synthetic_download_batch, synthetic_part,
                    live_fetch_source=live_fetch_source if live_source is None else None,
                )
                app.state.download = download
                platform_sender = live_sender if live_sender is not None else BilibiliSender(
                    bilibili_settings, gateway,
                )
                providers = {"mock": SyntheticModel(), "openai_compatible": ChatCompletions(client)}
                providers.update(model_providers or {})
                analysis = Analyze(database, app.state.llm_settings, providers, tasks)
                app.state.analysis = analysis

                async def execute(record: BatchWork):
                    if isinstance(record, SendWork):
                        return await execute_send(platform_sender, record)
                    if isinstance(record.inputs, ClassifyCreate):
                        return await analysis.execute(record)
                    if execute_batch is not None and record.inputs.mode == "mock":
                        return await execute_batch(record)
                    return await download.execute(record)

                worker = asyncio.create_task(Worker(tasks, execute, poll_interval, connection).run())
                monitor = asyncio.create_task(connection.monitor())
                renewal = asyncio.create_task(login.maintain(LOCAL_OWNER_ID))
                publications = asyncio.create_task(preload.run(LOCAL_OWNER_ID, max(poll_interval, 0.5)))
                try:
                    yield
                finally:
                    sessions.stop()
                    monitor.cancel()
                    renewal.cancel()
                    worker.cancel()
                    publications.cancel()
                    with suppress(asyncio.CancelledError):
                        await publications
                    with suppress(asyncio.CancelledError):
                        await worker
                    with suppress(asyncio.CancelledError):
                        await renewal
                    with suppress(asyncio.CancelledError):
                        await monitor
        finally:
            await run_in_threadpool(database.close)

    app = FastAPI(title="弹幕迁移工作台", version="0.1.0", lifespan=lifespan)
    app.state.sessions = sessions
    app.state.allowed_origins = frozenset(
        f"http://{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (5173, 8000)
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(workbench_router, prefix="/api/v1")
    app.include_router(inputs_router, prefix="/api/v1")
    app.include_router(analysis_router, prefix="/api/v1")
    app.include_router(review_router, prefix="/api/v1")
    app.include_router(sending_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(bilibili_login_router, prefix="/api/v1")
    app.include_router(publication_router, prefix="/api/v1")
    if static_dir is not None:
        app.mount("/yukkuri", StaticFiles(directory=static_dir.parent / "public" / "yukkuri", check_dir=False), name="yukkuri")
        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def workbench_index() -> FileResponse:
            return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/", StaticFiles(directory=static_dir, html=True), name="workbench")

    @app.exception_handler(WorkbenchError)
    async def use_case_error(_request: Request, exc: WorkbenchError) -> JSONResponse:
        return JSONResponse({"error": exc.info.model_dump()}, status_code=exc.status,
                            headers={"Cache-Control": "no-store"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return await use_case_error(_request, WorkbenchError(422, "invalid_fields", "请求字段格式无效"))

    return app

