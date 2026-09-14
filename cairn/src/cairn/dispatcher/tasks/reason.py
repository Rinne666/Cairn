from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_reason_payload
from cairn.dispatcher.prompting import (
    format_fact_ids,
    format_open_intents,
    load_prompt,
    render_prompt,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    did_timeout,
    preview,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_reason_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type, config.runtime.execution)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "checking worker health project=%s worker=%s timeout=%ss",
                project.project.id,
                worker.name,
                healthcheck_timeout,
            )
            health = driver.check_health(worker, timeout=healthcheck_timeout)
            if cancellation.is_cancelled:
                LOG.info(
                    "reason cancelled during healthcheck project=%s worker=%s reason=%s",
                    project.project.id,
                    worker.name,
                    cancellation.reason,
                )
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during reason healthcheck project=%s worker=%s status=%s",
                    project.project.id,
                    worker.name,
                    lease.failure.status_code,
                )
                return "failed"
            if not health.ok:
                LOG.warning(
                    "worker unhealthy project=%s worker=%s status=%s detail=%s",
                    project.project.id,
                    worker.name,
                    health.status,
                    health.detail,
                )
                return "unhealthy"
        open_intents = [
            {
                "id": intent.id,
                "from": intent.from_,
                "description": intent.description,
                "worker": intent.worker,
            }
            for intent in project.intents
            if intent.to is None
        ]
        allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
        LOG.debug(
            "reason context prepared project=%s worker=%s facts=%s allowed_fact_ids=%s hints=%s open_intents=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_intents),
        )
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "reason.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="reason_execute",
                ),
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_intents": format_open_intents(open_intents),
                "max_intents": str(config.tasks.reason.max_intents),
            },
        )

        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="reason_execute",
            timeout_seconds=config.tasks.reason.timeout,
            project_id=project.project.id,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "reason command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            reason_result = validate_reason_payload(
                payload, open_intents_empty=not open_intents, max_intents=config.tasks.reason.max_intents,
            )
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if reason_result.rejected:
            LOG.warning(
                "reason rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"
        if reason_result.complete is not None:
            complete_data = reason_result.complete
            response = client.complete(project.project.id, complete_data["from"], complete_data["description"], worker.name)
            if response.status_code == 403:
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning(
                    "reason complete write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                return "failed"
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                complete_data["from"],
                execute_ms,
                total_ms,
            )
            return "success"
        # Create normal intents (automated work).
        created = 0
        for intent_data in reason_result.intents:
            response = client.create_intent(project.project.id, intent_data["from"], intent_data["description"], worker.name)
            if response.status_code == 403:
                LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                return "success"
            if response.status_code == 409:
                LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                continue
            if not response.ok:
                LOG.warning(
                    "reason intent write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                continue
            created += 1
            LOG.info(
                "reason created intent project=%s worker=%s from=%s description=%s",
                project.project.id,
                worker.name,
                intent_data["from"],
                intent_data["description"],
            )
        # Create human interventions (auth requests) — never dispatched as Explore tasks.
        intervention_count = 0
        for intervention in reason_result.interventions:
            if intervention.get("type") != "auth":
                LOG.warning(
                    "reason skipped unsupported intervention project=%s worker=%s type=%s",
                    project.project.id,
                    worker.name,
                    intervention.get("type"),
                )
                intervention_count += 1  # handled: recognized and deliberately skipped
                continue
            auth_ref = intervention["target"]
            role = intervention["role"]
            # Authoritative role comes from config, not the LLM output, so a worker
            # cannot forge a role to bypass allow_roles. Enforce the allow-list here
            # (the server does not hold dispatch config).
            if config.auth is not None:
                try:
                    target_cfg = config.auth.target(auth_ref)
                except KeyError:
                    LOG.warning(
                        "reason auth request skipped unknown target project=%s worker=%s auth_ref=%s",
                        project.project.id,
                        worker.name,
                        auth_ref,
                    )
                    intervention_count += 1  # handled: recognized and deliberately skipped
                    continue
                role = target_cfg.role
                allowed = config.auth.intervention.allow_roles
                if allowed and role not in allowed:
                    LOG.warning(
                        "reason auth request blocked by allow_roles project=%s worker=%s auth_ref=%s role=%s allowed=%s",
                        project.project.id,
                        worker.name,
                        auth_ref,
                        role,
                        allowed,
                    )
                    intervention_count += 1  # handled: recognized and deliberately skipped
                    continue
            response = client.create_auth_request(
                project_id=project.project.id,
                source_fact_ids=intervention["from"],
                auth_ref=auth_ref,
                role=role,
                login_url=intervention.get("login_url"),
                reason=intervention["reason"],
            )
            if response.status_code == 409:
                # Dedup hit (valid session or in-flight request already exists): not an error.
                LOG.info(
                    "reason auth request deduped project=%s worker=%s auth_ref=%s",
                    project.project.id,
                    worker.name,
                    intervention["target"],
                )
                intervention_count += 1  # handled: dedup is a valid outcome
                continue
            if not response.ok:
                LOG.warning(
                    "reason auth request create failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                continue
            intervention_count += 1
            LOG.info(
                "reason created auth request project=%s worker=%s auth_ref=%s role=%s",
                project.project.id,
                worker.name,
                intervention["target"],
                intervention["role"],
            )
        if created == 0 and intervention_count == 0:
            if reason_result.is_noop:
                LOG.info(
                    "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    execute_ms,
                    total_ms,
                )
                return "success"
            LOG.warning(
                "reason created no intents or interventions project=%s worker=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
            )
            return "failed"
        LOG.info(
            "reason finished project=%s worker=%s created_intents=%s created_interventions=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            created,
            intervention_count,
            execute_ms,
            total_ms,
        )
        return "success"
    finally:
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name)
