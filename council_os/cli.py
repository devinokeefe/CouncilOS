from __future__ import annotations

import json
from pathlib import Path
from contextlib import redirect_stdout
import sys
from uuid import UUID, uuid4

import typer
import yaml

from council_os.agents.prompts.library import PromptLibrary
from council_os.agents.schemas import SCHEMA_VERSION
from council_os.agents.schemas import PlanPackage
from council_os.artifacts.diff import diff_artifacts
from council_os.audit.manifest import ManifestInput, create_manifest
from council_os.eval.harness import run_eval
from council_os.orchestrator.checkpoints import fork_from_checkpoint, list_checkpoints
from council_os.orchestrator.config_lint import lint_config
from council_os.orchestrator.engine import Engine
from council_os.orchestrator.feedback.config import FeedbackConfig
from council_os.orchestrator.feedback.gates import NeedsUserInput
from council_os.orchestrator.feedback.human_input import AutoProvider, CLIProvider, FileProvider, HumanInputProvider
from council_os.implementation.engine import ImplementationEngine
from council_os.orchestrator.hq_pipeline import HQPipeline, _git_code_version, _model_portfolio
from council_os.render import render_plan_markdown

app = typer.Typer(help="Council OS CLI")


def _engine(storage_root: Path) -> Engine:
    return Engine(storage_root=storage_root)


def _impl_engine(storage_root: Path) -> ImplementationEngine:
    return ImplementationEngine(storage_root=storage_root)


def _storage_root_from_config(config_path: Path) -> Path:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    runtime = raw.get("runtime", {}) if isinstance(raw, dict) else {}
    artifacts = runtime.get("artifacts", {}) if isinstance(runtime, dict) else {}
    store_dir = artifacts.get("store_dir") if isinstance(artifacts, dict) else None
    if isinstance(store_dir, str) and store_dir:
        base = store_dir.replace("{{run_id}}", "").replace("{run_id}", "")
        base = base.rstrip("/\\")
        if base.endswith("artifacts"):
            base = str(Path(base).parent)
        base_path = Path(base) if base else Path(".")
        if not base_path.is_absolute():
            base_path = (config_path.parent / base_path).resolve()
        return base_path
    storage_root = raw.get("storage_root", "CouncilOS/runs") if isinstance(raw, dict) else "CouncilOS/runs"
    if isinstance(storage_root, str):
        base_path = Path(storage_root)
        if not base_path.is_absolute():
            base_path = (config_path.parent / base_path).resolve()
        return base_path
    return (config_path.parent / Path("CouncilOS/runs")).resolve()


def _is_hq_config(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("stages") and raw.get("models") and raw.get("providers"))


NEEDS_USER_INPUT_CODE = 42


def _build_feedback(
    config: dict[str, object] | None,
    *,
    feedback_clarify: bool | None,
    feedback_plan_review: bool | None,
    feedback_provider: str | None,
    response_file: Path | None,
    max_plan_review_rounds: int | None,
) -> tuple[FeedbackConfig, HumanInputProvider]:
    cfg = FeedbackConfig.from_config(config or {})
    cfg.apply_overrides(
        clarify=feedback_clarify,
        plan_review=feedback_plan_review,
        provider=feedback_provider,
        max_plan_review_rounds=max_plan_review_rounds,
    )
    provider_key = (feedback_provider or ("file" if response_file else None) or cfg.provider or "auto").lower()
    if provider_key == "file":
        if response_file is None:
            raise typer.BadParameter("--response-file is required when feedback provider is 'file'")
        provider = FileProvider(response_file)
    elif provider_key == "cli":
        provider = CLIProvider()
    else:
        provider = AutoProvider()
    return cfg, provider

def _schema_version_from_hq_config(config: dict[str, object]) -> str:
    configured = str(config.get("schemas_version", "")).strip()
    if not configured:
        return SCHEMA_VERSION
    if configured != SCHEMA_VERSION:
        raise ValueError(
            f"schemas_version mismatch: config={configured}, runtime={SCHEMA_VERSION}. "
            "Update config or runtime schema version."
        )
    return configured


def _find_checkpoint_root(storage_root: Path, checkpoint_id: str) -> Path:
    for cp in storage_root.glob("*/checkpoints/*.json"):
        if cp.stem == checkpoint_id:
            return cp.parent.parent
    raise FileNotFoundError(f"Checkpoint not found: {checkpoint_id}")


def _fork_hq(checkpoint_id: str, new_config: Path) -> dict[str, object]:
    storage_root = _storage_root_from_config(new_config)
    config_raw = new_config.read_text(encoding="utf-8")
    config = yaml.safe_load(config_raw)
    if not isinstance(config, dict):
        raise ValueError("HQ config must be a mapping")
    if not _is_hq_config(config):
        raise ValueError("HQ fork requires an HQ config with stages/models/providers")

    source_run_root = _find_checkpoint_root(storage_root, checkpoint_id)
    brief_snapshot = source_run_root / "brief.snapshot.md"
    if not brief_snapshot.exists():
        raise FileNotFoundError("Run snapshots missing: brief.snapshot.md is required")

    workflow = config.get("workflow", {})
    if not isinstance(workflow, dict):
        workflow = {}
    orchestrator_cfg = config.get("runtime", {})
    if isinstance(orchestrator_cfg, dict):
        orchestrator_cfg = orchestrator_cfg.get("orchestrator", {})
    if not isinstance(orchestrator_cfg, dict):
        orchestrator_cfg = {}
    judge_policy = config.get("judge_policy", {})
    if not isinstance(judge_policy, dict):
        judge_policy = {}
    judge_rules = judge_policy.get("rules", {})
    if not isinstance(judge_rules, dict):
        judge_rules = {}
    judge_panel_size = judge_rules.get("panel_size", judge_policy.get("panel_size"))

    new_run_id = uuid4()
    new_run_root = storage_root / str(new_run_id)
    schema_version = _schema_version_from_hq_config(config)
    manifest = create_manifest(
        new_run_root,
        ManifestInput(
            run_id=new_run_id,
            code_version=_git_code_version(),
            schema_versions={"artifacts": schema_version},
            run_config_version=str(config.get("version", "")),
            stage_machine_version=str(config.get("version", "1.0.0")),
            validator_suite_version=str(config.get("version", "1.0.0")),
            selection_protocol_version=str(config.get("version", "1.0.0")),
            model_portfolio=_model_portfolio(config),
            branching_policy={
                "branch_count": workflow.get("branches_max"),
                "keep_count": workflow.get("branches_kept_after_prune"),
            },
            ensemble_policy={
                "synthesizers_per_branch": workflow.get("candidates_per_branch"),
                "judge_panel_size": judge_panel_size,
            },
            iteration_caps={
                "max_full_cycles_without_pass": orchestrator_cfg.get("max_full_cycles_without_pass"),
                "max_pod_reruns": orchestrator_cfg.get("max_pod_reruns"),
                "max_branch_rebuilds": orchestrator_cfg.get("max_branch_rebuilds"),
                "max_candidate_repairs": orchestrator_cfg.get("max_repairs_per_candidate"),
            },
            tool_policy_version="1.0.0",
            tools_enabled=False,
            consent_profile="trusted_user",
            determinism_disclaimer="LLM outputs are stochastic; orchestrator routing is deterministic.",
            prompt_pack_hash=PromptLibrary().pack_hash(),
            config_raw=config_raw,
            parent_run_id=UUID(source_run_root.name),
            forked_from_checkpoint_id=checkpoint_id,
        ),
    )
    fork_result = fork_from_checkpoint(source_run_root, checkpoint_id, new_run_root, manifest)
    (new_run_root / "config.snapshot.yml").write_text(config_raw, encoding="utf-8")
    (new_run_root / "brief.snapshot.md").write_text(
        brief_snapshot.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {
        "run_id": str(new_run_id),
        "run_root": str(new_run_root),
        "checkpoint_id": fork_result.checkpoint_id,
    }


@app.command()
def run(
    config: Path = typer.Option(...),
    brief: Path = typer.Option(...),
    feedback_clarify: bool | None = typer.Option(
        None,
        "--feedback-clarify/--no-feedback-clarify",
        help="Enable or disable the Clarify Intent Gate.",
    ),
    feedback_plan_review: bool | None = typer.Option(
        None,
        "--feedback-plan-review/--no-feedback-plan-review",
        help="Enable or disable the Plan Review Gate.",
    ),
    feedback_provider: str | None = typer.Option(
        None,
        "--feedback-provider",
        help="Feedback provider: auto, cli, or file.",
    ),
    response_file: Path | None = typer.Option(
        None,
        "--response-file",
        help="Path to response artifact JSON for file provider.",
    ),
    max_plan_review_rounds: int | None = typer.Option(
        None,
        "--max-plan-review-rounds",
        help="Maximum plan review rounds before pausing.",
    ),
) -> None:
    storage_root = _storage_root_from_config(config)
    raw_text = config.read_text(encoding="utf-8")
    raw = yaml.safe_load(raw_text)
    engine = _engine(storage_root)
    feedback_cfg, feedback_input = _build_feedback(
        raw if isinstance(raw, dict) else {},
        feedback_clarify=feedback_clarify,
        feedback_plan_review=feedback_plan_review,
        feedback_provider=feedback_provider,
        response_file=response_file,
        max_plan_review_rounds=max_plan_review_rounds,
    )
    try:
        if isinstance(raw, dict) and raw.get("stages") and raw.get("models") and raw.get("providers"):
            pipeline = HQPipeline(storage_root=storage_root, config_path=config, config_raw=raw_text, config=raw)
            with _redirect_engine_output():
                result = pipeline.run(
                    brief_path=brief,
                    feedback_config=feedback_cfg,
                    feedback_provider=feedback_input,
                )
        else:
            with _redirect_engine_output():
                result = engine.run(
                    brief_path=brief,
                    config_path=config,
                    feedback_config=feedback_cfg,
                    feedback_provider=feedback_input,
                )
    except NeedsUserInput as exc:
        payload = {
            "run_id": str(exc.run_id),
            "pending_action": str(exc.pending_action_path),
        }
        typer.echo(json.dumps(payload))
        raise typer.Exit(code=NEEDS_USER_INPUT_CODE)
    payload = {"run_id": str(result.run_id), "frozen_artifact_id": result.frozen_artifact_id}
    try:
        metrics = engine.show_artifact(str(result.run_id), "run_metrics_v1")["payload"]
        payload["metrics"] = {
            "candidate_count": metrics.get("candidate_count"),
            "repaired_candidate_count": metrics.get("repaired_candidate_count"),
            "validator_pass_count": metrics.get("validator_pass_count"),
            "validator_fail_count": metrics.get("validator_fail_count"),
            "total_duration_sec": metrics.get("total_duration_sec"),
        }
    except Exception:
        pass
    typer.echo(json.dumps(payload))


@app.command()
def implement(
    config: Path = typer.Option(...),
    plan: Path | None = typer.Option(None, "--plan"),
    handoff_bundle: Path | None = typer.Option(None, "--handoff-bundle"),
    repo_context: Path | None = typer.Option(None, "--repo-context"),
    workspace_context: Path | None = typer.Option(None, "--workspace-context"),
    from_handoff: Path | None = typer.Option(
        None,
        "--from-handoff",
        help="Planning handoff bundle path; derives plan and context inputs automatically.",
    ),
    allow_repo_override: bool = typer.Option(
        False,
        "--allow-repo-override/--no-allow-repo-override",
        help="Allow repo snapshot mismatch during handoff acceptance (recorded).",
    ),
) -> None:
    storage_root = _storage_root_from_config(config)
    engine = _impl_engine(storage_root)
    if from_handoff is None:
        if plan is None or handoff_bundle is None or repo_context is None or workspace_context is None:
            raise typer.BadParameter(
                "--plan, --handoff-bundle, --repo-context, and --workspace-context are required when --from-handoff is not set"
            )
        result = engine.run(
            plan,
            repo_context,
            workspace_context,
            config,
            handoff_bundle,
            allow_repo_override=allow_repo_override,
        )
    else:
        dummy = Path(".")
        result = engine.run(
            plan or dummy,
            repo_context or dummy,
            workspace_context or dummy,
            config,
            handoff_bundle or from_handoff,
            from_handoff=from_handoff,
            allow_repo_override=allow_repo_override,
        )
    payload = {"run_id": str(result.run_id), "run_root": str(result.run_root)}
    typer.echo(json.dumps(payload))


@app.command()
def resume(
    run_id: str = typer.Option(..., "--run-id", "--resume-run"),
    checkpoint_id: str | None = typer.Option(None, "--checkpoint-id"),
    storage_root: Path = typer.Option(Path("CouncilOS/runs"), "--storage-root"),
    feedback_clarify: bool | None = typer.Option(
        None,
        "--feedback-clarify/--no-feedback-clarify",
        help="Enable or disable the Clarify Intent Gate.",
    ),
    feedback_plan_review: bool | None = typer.Option(
        None,
        "--feedback-plan-review/--no-feedback-plan-review",
        help="Enable or disable the Plan Review Gate.",
    ),
    feedback_provider: str | None = typer.Option(
        None,
        "--feedback-provider",
        help="Feedback provider: auto, cli, or file.",
    ),
    response_file: Path | None = typer.Option(
        None,
        "--response-file",
        help="Path to response artifact JSON for file provider.",
    ),
    max_plan_review_rounds: int | None = typer.Option(
        None,
        "--max-plan-review-rounds",
        help="Maximum plan review rounds before pausing.",
    ),
) -> None:
    run_root = storage_root / run_id
    config_snapshot = run_root / "config.snapshot.yml"
    if config_snapshot.exists():
        raw_text = config_snapshot.read_text(encoding="utf-8")
        raw = yaml.safe_load(raw_text)
        feedback_cfg, feedback_input = _build_feedback(
            raw if isinstance(raw, dict) else {},
            feedback_clarify=feedback_clarify,
            feedback_plan_review=feedback_plan_review,
            feedback_provider=feedback_provider,
            response_file=response_file,
            max_plan_review_rounds=max_plan_review_rounds,
        )
        if isinstance(raw, dict) and raw.get("stages") and raw.get("models") and raw.get("providers"):
            pipeline = HQPipeline(
                storage_root=storage_root,
                config_path=config_snapshot,
                config_raw=raw_text,
                config=raw,
                run_id=UUID(run_id),
                run_root=run_root,
            )
            try:
                with _redirect_engine_output():
                    result = pipeline.resume(
                        checkpoint_id=checkpoint_id,
                        feedback_config=feedback_cfg,
                        feedback_provider=feedback_input,
                    )
            except NeedsUserInput as exc:
                payload = {
                    "run_id": str(exc.run_id),
                    "pending_action": str(exc.pending_action_path),
                }
                typer.echo(json.dumps(payload))
                raise typer.Exit(code=NEEDS_USER_INPUT_CODE)
        else:
            engine = _engine(storage_root)
            try:
                with _redirect_engine_output():
                    result = engine.resume(
                        run_id=run_id,
                        checkpoint_id=checkpoint_id,
                        feedback_config=feedback_cfg,
                        feedback_provider=feedback_input,
                    )
            except NeedsUserInput as exc:
                payload = {
                    "run_id": str(exc.run_id),
                    "pending_action": str(exc.pending_action_path),
                }
                typer.echo(json.dumps(payload))
                raise typer.Exit(code=NEEDS_USER_INPUT_CODE)
    else:
        engine = _engine(storage_root)
        feedback_cfg, feedback_input = _build_feedback(
            {},
            feedback_clarify=feedback_clarify,
            feedback_plan_review=feedback_plan_review,
            feedback_provider=feedback_provider,
            response_file=response_file,
            max_plan_review_rounds=max_plan_review_rounds,
        )
        try:
            with _redirect_engine_output():
                result = engine.resume(
                    run_id=run_id,
                    checkpoint_id=checkpoint_id,
                    feedback_config=feedback_cfg,
                    feedback_provider=feedback_input,
                )
        except NeedsUserInput as exc:
            payload = {
                "run_id": str(exc.run_id),
                "pending_action": str(exc.pending_action_path),
            }
            typer.echo(json.dumps(payload))
            raise typer.Exit(code=NEEDS_USER_INPUT_CODE)
    payload = {"run_id": str(result.run_id), "frozen_artifact_id": result.frozen_artifact_id}
    try:
        metrics = _engine(storage_root).show_artifact(str(result.run_id), "run_metrics_v1")["payload"]
        payload["metrics"] = {
            "candidate_count": metrics.get("candidate_count"),
            "repaired_candidate_count": metrics.get("repaired_candidate_count"),
            "validator_pass_count": metrics.get("validator_pass_count"),
            "validator_fail_count": metrics.get("validator_fail_count"),
            "total_duration_sec": metrics.get("total_duration_sec"),
        }
    except Exception:
        pass
    typer.echo(json.dumps(payload))


@app.command()
def fork(
    checkpoint_id: str = typer.Option(..., "--checkpoint-id"),
    new_config: Path = typer.Option(..., "--new-config"),
) -> None:
    config_raw = new_config.read_text(encoding="utf-8")
    config = yaml.safe_load(config_raw)
    if _is_hq_config(config):
        payload = _fork_hq(checkpoint_id, new_config)
        typer.echo(json.dumps(payload))
        return
    result = _engine(_storage_root_from_config(new_config)).fork(
        checkpoint_id=checkpoint_id,
        new_config_path=new_config,
    )
    typer.echo(json.dumps({"run_id": str(result.run_id), "run_root": str(result.run_root)}))


@app.command()
def show(
    run_id: str = typer.Option(..., "--run-id"),
    artifact: str = typer.Option(..., "--artifact"),
    storage_root: Path = typer.Option(Path("CouncilOS/runs"), "--storage-root"),
    render: bool = typer.Option(False, "--render"),
) -> None:
    payload = _engine(storage_root).show_artifact(run_id=run_id, artifact_id=artifact)
    if render:
        plan_payload = payload.get("payload", {}).get("plan_package")
        if isinstance(plan_payload, dict):
            try:
                plan = PlanPackage.model_validate(plan_payload)
                typer.echo(render_plan_markdown(plan))
                return
            except Exception:
                pass
        run_root = storage_root / run_id
        markdown = run_root / "frozen_plan.md"
        if markdown.exists():
            typer.echo(markdown.read_text(encoding="utf-8"))
            return
    typer.echo(json.dumps(payload, indent=2, default=str))


@app.command("list")
def list_artifacts(
    run_id: str = typer.Option(..., "--run-id"),
    artifacts: bool = typer.Option(False, "--artifacts"),
    storage_root: Path = typer.Option(Path("CouncilOS/runs"), "--storage-root"),
) -> None:
    if not artifacts:
        raise typer.BadParameter("Use --artifacts")
    rows = _engine(storage_root).list_artifacts(run_id=run_id)
    typer.echo(json.dumps(rows, indent=2))


@app.command()
def eval(config: Path = typer.Option(...), golden: Path = typer.Option(...)) -> None:
    metrics = run_eval(_engine(_storage_root_from_config(config)), config, golden)
    typer.echo(
        json.dumps(
            {
                "total_briefs": metrics.total_briefs,
                "successful_runs": metrics.successful_runs,
                "pass_rate": metrics.pass_rate,
                "invariant_passed_runs": metrics.invariant_passed_runs,
                "invariant_pass_rate": metrics.invariant_pass_rate,
                "avg_stage_duration_sec": metrics.avg_stage_duration_sec,
                "avg_events_per_run": metrics.avg_events_per_run,
                "avg_judge_agreement": metrics.avg_judge_agreement,
                "judge_agreement_drift": metrics.judge_agreement_drift,
                "blocker_spike_runs": metrics.blocker_spike_runs,
                "blocker_spike_rate": metrics.blocker_spike_rate,
            }
        )
    )


@app.command()
def diff(
    run_id: str = typer.Option(..., "--run-id"),
    a: str = typer.Option(..., "--a"),
    b: str = typer.Option(..., "--b"),
    storage_root: Path = typer.Option(Path("CouncilOS/runs"), "--storage-root"),
) -> None:
    engine = _engine(storage_root)
    left = engine.show_artifact(run_id=run_id, artifact_id=a)
    right = engine.show_artifact(run_id=run_id, artifact_id=b)
    typer.echo(json.dumps(diff_artifacts(left, right), indent=2, default=str))


@app.command()
def status(
    run_id: str = typer.Option(..., "--run-id"),
    storage_root: Path = typer.Option(Path("CouncilOS/runs"), "--storage-root"),
) -> None:
    run_root = storage_root / run_id
    config_snapshot = run_root / "config.snapshot.yml"
    if config_snapshot.exists():
        raw_text = config_snapshot.read_text(encoding="utf-8")
        raw = yaml.safe_load(raw_text)
        if isinstance(raw, dict) and raw.get("stages") and raw.get("models") and raw.get("providers"):
            pipeline = HQPipeline(
                storage_root=storage_root,
                config_path=config_snapshot,
                config_raw=raw_text,
                config=raw,
                run_id=UUID(run_id),
                run_root=run_root,
            )
            typer.echo(json.dumps(pipeline.status(), indent=2, default=str))
            return
    typer.echo(json.dumps(_engine(storage_root).status(run_id), indent=2, default=str))


@app.command()
def checkpoints(
    run_id: str = typer.Option(..., "--run-id"),
    storage_root: Path = typer.Option(Path("CouncilOS/runs"), "--storage-root"),
) -> None:
    run_root = storage_root / run_id
    items = list_checkpoints(run_root)
    payload = [
        {
            "checkpoint_id": item.checkpoint_id,
            "created_at": item.created_at.isoformat(),
            "stage_name": item.stage_name,
            "stage_index": item.stage_index,
        }
        for item in items
    ]
    typer.echo(json.dumps(payload, indent=2))


@app.command()
def lint(config: Path = typer.Option(..., "--config")) -> None:
    errors = lint_config(config)
    typer.echo(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    if errors:
        raise typer.Exit(code=1)


def _redirect_engine_output():
    return redirect_stdout(sys.stderr)


if __name__ == "__main__":
    app()
