"""Execute durable matching tasks; completion prepares a revision, never publishes it."""
import hashlib
from pathlib import Path

from .matching_service import run_matching


def pipeline_fingerprint():
    package = Path(__file__).parent
    files = ("matching_service.py", "matching.py", "candidates.py", "normalization.py", "resolution.py",
             "incremental.py", "identity.py", "store.py", "query_projection.py", "worker.py",
             "ditto.py", "vendor/ditto_model.py")
    pipeline = hashlib.sha256()
    for name in files:
        pipeline.update(name.encode() + b"\0" + (package / name).read_bytes())
    return pipeline.hexdigest()


def model_fingerprints(settings, model_path=None, candidate_model_path=None):
    result = {"method": settings.method, "candidate_mode": settings.candidate_mode,
              "pipeline_sha256": pipeline_fingerprint()}
    if settings.method == "splink":
        if model_path is None:
            raise ValueError("A frozen Splink model must be configured")
        from .matching import SplinkMatcher
        result["model"] = SplinkMatcher.load(model_path).fingerprint
    if settings.method == "ditto":
        if model_path is None:
            raise ValueError("A frozen company-domain Ditto model must be configured")
        from .ditto import bundle_manifest
        manifest = bundle_manifest(model_path)
        if manifest["domain"] != "company":
            raise ValueError("Company matching requires a company-domain Ditto model")
        result["model"] = manifest["fingerprint"]
    if settings.candidate_mode == "hybrid":
        if candidate_model_path is None:
            raise ValueError("A frozen candidate model must be configured")
        from .candidates import FrozenNameRetriever
        result["candidate_model"] = FrozenNameRetriever.load(candidate_model_path).fingerprint
    return result


def execute_matching_job(store, queue, lease, *, model_path=None, candidate_model_path=None):
    from .api import RunRequest
    from .jobs import StaleJob
    settings = RunRequest.model_validate(lease.payload["settings"])
    if model_fingerprints(settings, model_path, candidate_model_path) != lease.payload["models"]:
        raise StaleJob("Configured model or pipeline differs from the submitted task")
    def guard(con):
        if pipeline_fingerprint() != lease.payload["models"]["pipeline_sha256"]:
            raise StaleJob("Pipeline source changed during execution; restart with an immutable installation")
        queue.validate_basis(con, lease)
    def bind(con, revision_id):
        if pipeline_fingerprint() != lease.payload["models"]["pipeline_sha256"]:
            raise StaleJob("Pipeline source changed before commit")
        queue.bind_prepared(con, lease, revision_id)
    return run_matching(store, settings, model_path, candidate_model_path,
                        basis_guard=guard, expected_models=lease.payload["models"],
                        commit_hook=bind)
