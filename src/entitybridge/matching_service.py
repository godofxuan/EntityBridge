"""Matching orchestration shared by synchronous requests and durable workers."""
from .candidates import BLOCKING_VERSION, FrozenNameRetriever, generate_candidates
from .matching import score_baselines
from .normalization import FEATURE_VIEW_VERSION, NORMALIZATION_VERSION, matcher_view
from .store import digest


def run_matching(store, settings, model_path=None, candidate_model_path=None, *, basis_guard=None, commit_hook=None,
                 expected_models=None):
    raw = store.active_records()
    records = [matcher_view(row, key, row["record_version_id"]) for key, row in raw.items()]
    retriever = None
    if settings.candidate_mode == "hybrid":
        if not candidate_model_path:
            raise ValueError("Hybrid review candidates require a frozen training-only candidate model")
        retriever = FrozenNameRetriever.load(candidate_model_path)
    if settings.method == "splink":
        if not model_path:
            raise ValueError("A frozen trained model must be configured before a Splink run")
        from .matching import SplinkMatcher
        model = SplinkMatcher.load(model_path)
        score = model.score
        model_version = model.fingerprint
    elif settings.method == "ditto":
        if not model_path:
            raise ValueError("A frozen company-domain Ditto model must be configured")
        from .ditto import DittoMatcher
        model = DittoMatcher.load(model_path)
        if model.manifest["domain"] != "company":
            raise ValueError("Company matching requires a company-domain Ditto model")
        score = model.score
        model_version = model.fingerprint
    else:
        score = lambda rows, pairs: score_baselines(rows, pairs)[settings.method]
        model_version = settings.method + "-name-v1"
    if expected_models is not None:
        from .jobs import StaleJob
        if ((settings.method in {"splink", "ditto"} and model_version != expected_models.get("model"))
                or (retriever is not None and retriever.fingerprint != expected_models.get("candidate_model"))):
            raise StaleJob("Loaded matching model differs from the submitted frozen model")
    review_only = retriever is not None or settings.method == "ditto"
    policy = digest({"model": model_version, "threshold": settings.threshold,
        "normalization": NORMALIZATION_VERSION, "feature_view": FEATURE_VIEW_VERSION,
        "review_threshold": settings.review_threshold, "blocking": BLOCKING_VERSION, "resolver": "greedy-v2-manual-status",
        "candidate_mode": settings.candidate_mode, "candidate_model": retriever.fingerprint if retriever else None,
        "automatic_merge": not review_only})
    parent = store.current_revision()
    previous = store._payload(parent) if parent else None
    refresh = None
    if settings.incremental and not review_only and previous and previous["policy_version"] == policy:
        from .incremental import refresh_scores
        old_records = [matcher_view(row, key, row["record_version_id"]) for key, row in previous["records"].items()]
        edges, refresh = refresh_scores(old_records, records, previous["edges"], score)
        if settings.verify_full:
            complete = score(records, generate_candidates(records))
            as_map = lambda items: {(edge["left"], edge["right"]): edge["score"] for edge in items}
            a, b = as_map(edges), as_map(complete)
            if a.keys() != b.keys() or any(abs(a[pair] - b[pair]) > 1e-12 for pair in a):
                raise RuntimeError("Incremental scores differ from full scoring")
    else:
        candidates = generate_candidates(records, retriever=retriever)
        edges = score(records, candidates)
    if review_only:
        edges = [{**edge, "auto_merge": False} for edge in edges]
    candidate = store.prepare_revision(edges, policy_version=policy, threshold=settings.threshold,
        review_threshold=settings.review_threshold, expected_input_hash=digest(raw), verify_full=settings.verify_full,
        force_full=review_only, force_full_reason="global_candidate_index" if retriever else "neural_review_candidates",
        basis_guard=basis_guard, commit_hook=commit_hook)
    return {**candidate, "candidate_pairs": len(edges), "records": len(records), "method": settings.method,
            "score_refresh": refresh, "candidate_mode": settings.candidate_mode,
            "automatic_merge": not review_only}

