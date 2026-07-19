"""Pipeline-worker dispatcher.

In production: enqueues a Cloud Run Job execution that runs the
``backend.pipeline_worker`` entrypoint with project_id/user_id/resume/free
passed as env-var overrides.

In local dev (no ``GCS_BUCKET``): runs the pipeline as an in-process
asyncio task so it can be debugged with a normal debugger.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from backend.config import settings

logger = logging.getLogger(__name__)


ExecutionState = Literal[
    "pending", "running", "succeeded", "failed", "cancelled", "unknown"
]


# ---------------------------------------------------------------------------
# Local async-task path (dev mode)
# ---------------------------------------------------------------------------


# Track in-process tasks so the API can query "is it still running?" in
# dev. In prod the Cloud Run Jobs admin API answers the same question.
_local_tasks: dict[str, asyncio.Task[None]] = {}


def _local_execution_name(project_id: str) -> str:
    """Stable synthetic execution name for the dev in-process path.

    Lets the rest of the codebase treat dev runs uniformly with prod
    runs (we always have an ``execution_name`` to store on ProjectMeta
    and pass to status / cancel calls).
    """
    return f"local/projects/{project_id}"


async def _task_wrapper(
    project_id: str,
    user_id: str,
    *,
    resume: bool,
    free: bool,
    mode: str,
    regen_stages: list[str] | None,
) -> None:
    """Run the pipeline in-process with the GCS-backed event broker.

    The SSE handler tails events from storage via ``tail_events``.
    We temporarily swap the module-level broker so events written by
    the pipeline end up in storage rather than the in-memory queues.
    """
    from backend.services import event_bridge as event_bridge
    from backend.services import pipeline as pipeline_svc
    from backend.services.storage import LocalStorageBackend

    storage = LocalStorageBackend(settings.data_dir)
    original_broker = pipeline_svc.broker
    pipeline_svc.set_broker(event_bridge.GCSEventBroker(storage, user_id))

    try:
        # Fresh runs wipe the prior event log so the SSE consumer doesn't
        # mix old events into the new run. Resume keeps the prior log so
        # users see the full history.
        if not resume:
            pipeline_svc.broker.clear_history(project_id)

        if mode == "run":
            await pipeline_svc.run_pipeline(
                storage, user_id, project_id, resume=resume, free=free,
            )
        elif mode == "regen":
            if not regen_stages:
                raise ValueError("regen mode requires at least one stage")
            await pipeline_svc.run_regen_pipeline(
                storage, user_id, project_id, regen_stages,
            )
        else:
            raise ValueError(f"unknown mode {mode!r}; expected 'run' or 'regen'")
    except asyncio.CancelledError:
        # Task was cancelled (e.g. via cancel_execution). Publish a
        # terminal event so the SSE tailer doesn't hang forever.
        try:
            pipeline_svc.broker.publish(
                project_id, "pipeline_cancelled",
                {"error": "pipeline cancelled by user"},
            )
        except Exception:
            logger.exception("failed to publish cancel event for %s", project_id)
        raise
    finally:
        pipeline_svc.set_broker(original_broker)


def _enqueue_local_task(
    project_id: str,
    user_id: str,
    *,
    resume: bool,
    free: bool,
    mode: str = "run",
    regen_stages: list[str] | None = None,
) -> str:
    name = _local_execution_name(project_id)

    # Cancel any prior task for the same project before starting a new one.
    prior = _local_tasks.pop(project_id, None)
    if prior is not None and not prior.done():
        prior.cancel()

    task: asyncio.Task[None] = asyncio.create_task(
        _task_wrapper(
            project_id, user_id,
            resume=resume, free=free,
            mode=mode, regen_stages=regen_stages,
        )
    )
    _local_tasks[project_id] = task
    logger.info("dev: spawned in-process pipeline task for %s", project_id)
    return name


def _local_state(project_id: str) -> ExecutionState:
    task = _local_tasks.get(project_id)
    if task is None:
        return "unknown"
    if not task.done():
        return "running"
    if task.cancelled():
        return "cancelled"
    exc = task.exception()
    if exc is not None:
        return "failed"
    return "succeeded"


def _local_cancel(project_id: str) -> None:
    task = _local_tasks.get(project_id)
    if task is None or task.done():
        return
    try:
        task.cancel()
    except Exception:
        logger.exception("dev: failed to cancel pipeline task for %s", project_id)


# ---------------------------------------------------------------------------
# Cloud Run Jobs (prod path)
# ---------------------------------------------------------------------------


def _gcp_project() -> str:
    """Resolve the GCP project id for the Cloud Run Jobs admin API."""
    if settings.pipeline_worker_project:
        return settings.pipeline_worker_project
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
    if proj:
        return proj
    # Fall back to the metadata server (works on Cloud Run).
    try:
        import requests  # type: ignore[import-not-found]

        resp = requests.get(
            "http://metadata.google.internal/computeMetadata/v1/project/project-id",
            headers={"Metadata-Flavor": "Google"},
            timeout=2.0,
        )
        if resp.ok:
            return resp.text.strip()
    except Exception:
        pass
    raise RuntimeError(
        "Could not resolve GCP project for Cloud Run Jobs. Set "
        "PIPELINE_WORKER_PROJECT or GOOGLE_CLOUD_PROJECT."
    )


def _job_resource_name() -> str:
    return (
        f"projects/{_gcp_project()}/locations/{settings.pipeline_worker_region}"
        f"/jobs/{settings.pipeline_worker_job_name}"
    )


def _jobs_client():
    # Lazy import: keeps the API process startup fast in local dev where
    # google-cloud-run isn't even installed (it's an optional dep there).
    from google.cloud import run_v2  # type: ignore[import-not-found]

    return run_v2.JobsClient()


def _executions_client():
    from google.cloud import run_v2  # type: ignore[import-not-found]

    return run_v2.ExecutionsClient()


def _enqueue_cloud_run_job(
    project_id: str,
    user_id: str,
    *,
    resume: bool,
    free: bool,
    mode: str = "run",
    regen_stages: list[str] | None = None,
) -> str:
    """Issue ``RunJob`` with env-var overrides; return the execution name."""
    from google.cloud import run_v2  # type: ignore[import-not-found]

    env_overrides = [
        run_v2.EnvVar(name="PROJECT_ID", value=project_id),
        run_v2.EnvVar(name="USER_ID", value=user_id),
        run_v2.EnvVar(name="RESUME", value="1" if resume else "0"),
        run_v2.EnvVar(name="FREE", value="1" if free else "0"),
        run_v2.EnvVar(name="MODE", value=mode),
    ]
    if regen_stages:
        env_overrides.append(
            run_v2.EnvVar(name="REGEN_STAGES", value=",".join(regen_stages)),
        )
    overrides = run_v2.RunJobRequest.Overrides(
        container_overrides=[
            run_v2.RunJobRequest.Overrides.ContainerOverride(env=env_overrides),
        ],
    )
    request = run_v2.RunJobRequest(name=_job_resource_name(), overrides=overrides)
    operation = _jobs_client().run_job(request=request)
    # Don't wait for completion — fire and forget. The metadata is enough
    # to extract the execution resource name.
    metadata = operation.metadata
    name = getattr(metadata, "name", None) if metadata is not None else None
    if not name:
        # As a fallback, peek at the operation; on Cloud Run RunJob this
        # is a long-running op whose initial metadata holds the execution.
        name = operation.operation.name  # type: ignore[union-attr]
    if not name:
        raise RuntimeError("Cloud Run RunJob returned no execution name")
    logger.info("enqueued Cloud Run Job execution %s for project %s", name, project_id)
    return name


def _cloud_run_state(execution_name: str) -> ExecutionState:
    """Map Cloud Run Execution state to our enum."""
    try:
        from google.cloud import run_v2  # type: ignore[import-not-found]

        client = _executions_client()
        ex = client.get_execution(name=execution_name)
    except Exception:
        logger.exception("get_execution failed for %s", execution_name)
        return "unknown"

    # An Execution has reconciliation_started, completion_time, conditions.
    # Map to our enum based on completion + conditions.
    if ex.completion_time is None or ex.completion_time.seconds == 0:
        if ex.start_time and ex.start_time.seconds:
            return "running"
        return "pending"
    # Completed — figure out success vs failure.
    failed = int(getattr(ex, "failed_count", 0) or 0)
    cancelled = int(getattr(ex, "cancelled_count", 0) or 0)
    succeeded = int(getattr(ex, "succeeded_count", 0) or 0)
    if cancelled > 0 and succeeded == 0:
        return "cancelled"
    if failed > 0:
        return "failed"
    if succeeded > 0:
        return "succeeded"
    return "unknown"


def _cloud_run_cancel(execution_name: str) -> None:
    try:
        from google.cloud import run_v2  # type: ignore[import-not-found]

        request = run_v2.CancelExecutionRequest(name=execution_name)
        _executions_client().cancel_execution(request=request)
    except Exception:
        logger.exception("cancel_execution failed for %s", execution_name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def use_cloud_run_jobs() -> bool:
    """True iff we should dispatch via Cloud Run Jobs.

    Tied to whether GCS storage is configured — Jobs and GCS go together
    in prod, and local dev uses neither.
    """
    return bool(settings.gcs_bucket)


def enqueue_pipeline(
    project_id: str, user_id: str, *, resume: bool = False, free: bool = False,
) -> str:
    """Dispatch a pipeline run.

    In prod, returns the Cloud Run Execution resource name. In dev,
    returns a synthetic ``local/projects/{id}`` name. Either way, callers
    should persist the returned name on ``ProjectMeta.execution_name``.
    """
    if use_cloud_run_jobs():
        return _enqueue_cloud_run_job(project_id, user_id, resume=resume, free=free)
    return _enqueue_local_task(project_id, user_id, resume=resume, free=free)


def enqueue_pipeline_regen(
    project_id: str, user_id: str, *, stages: list[str],
) -> str:
    """Dispatch a regen run (graph + selected stages, free).

    Same image, same worker; differs only in the env-var-driven mode.
    """
    if not stages:
        raise ValueError("regen requires at least one stage")
    if use_cloud_run_jobs():
        return _enqueue_cloud_run_job(
            project_id, user_id, resume=False, free=True,
            mode="regen", regen_stages=stages,
        )
    return _enqueue_local_task(
        project_id, user_id, resume=False, free=True,
        mode="regen", regen_stages=stages,
    )


def get_execution_state(execution_name: str | None) -> ExecutionState:
    """Return current state of a previously-enqueued execution.

    Used by the SSE handler's hard-crash escape hatch and by the
    stale-running sweeper. ``None`` -> ``"unknown"``.
    """
    if not execution_name:
        return "unknown"
    if execution_name.startswith("local/projects/"):
        project_id = execution_name.split("/", 2)[-1]
        return _local_state(project_id)
    return _cloud_run_state(execution_name)


def cancel_execution(execution_name: str | None) -> None:
    """Hard-cancel an execution (Cloud Run cancel or local SIGTERM).

    Best-effort. Soft cancel via ``meta.cancel_requested`` is preferred —
    only fall back to this when the worker has already gone unresponsive.
    """
    if not execution_name:
        return
    if execution_name.startswith("local/projects/"):
        project_id = execution_name.split("/", 2)[-1]
        _local_cancel(project_id)
        return
    _cloud_run_cancel(execution_name)
