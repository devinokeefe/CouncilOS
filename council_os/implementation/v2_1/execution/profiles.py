from __future__ import annotations

from council_os.implementation.schemas import ExecutionProfileCatalogPayload, RepoContextPayloadV21


def default_profile_id(repo_context: RepoContextPayloadV21, catalog: ExecutionProfileCatalogPayload) -> str:
    default_profile = None
    if repo_context.execution_profiles:
        defaults = repo_context.execution_profiles.default_profile_id_by_stage
        for key in ("WorkPlanning", "Generate", "Validate", "IntegrateAndValidate", "Review"):
            if key in defaults:
                default_profile = defaults[key]
                break
        if not default_profile and repo_context.execution_profiles.allowed_profile_ids:
            default_profile = sorted(repo_context.execution_profiles.allowed_profile_ids)[0]
    if not default_profile and catalog.profiles:
        default_profile = sorted([p.profile_id for p in catalog.profiles])[0]
    if not default_profile:
        raise ValueError("No execution profiles available")
    return default_profile
