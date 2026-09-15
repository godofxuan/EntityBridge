"""Matching orchestration shared by synchronous requests and durable workers."""
from .candidates import BLOCKING_VERSION, FrozenNameRetriever, generate_candidates
from .country import country_candidate_view, country_fingerprint
from .matching import score_baselines
from .normalization import FEATURE_VIEW_VERSION, NORMALIZATION_VERSION, matcher_view
from .store import digest


def run_matching(store, settings, model_path=None, candidate_model_path=None, *, basis_guard=None, commit_hook=None,
                 expected_models=None):
    raw = store.active_records()
    records = [matcher_view(row, key, row["record_version_id"]) for key, row in raw.items()]
    retriever = None
    iso_country = settings.candidate_mode in {"fixed_iso", "hybrid_iso"}
    if settings.candidate_mode in {"hybrid", "hybrid_iso"}:
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
    elif settings.method == "company":
        if not model_path:
            raise ValueError("A frozen company feature model must be configured")
        from .company_matching import FrozenCompanyMatcher
        model = FrozenCompanyMatcher.load(model_path)
        score = model.score
        model_version = model.fingerprint
    else:
        score = lambda rows, pairs: score_baselines(rows, pairs)[settings.method]
        model_version = settings.method + "-name-v1"
    if expected_models is not None:
        from .jobs import StaleJob
        if ((settings.method in {"splink", "ditto", "company"} and model_version != expected_models.get("model"))
                or (retriever is not None and retriever.fingerprint != expected_models.get("candidate_model"))):
            raise StaleJob("Loaded matching model differs from the submitted frozen model")
    review_only = retriever is not None or iso_country or settings.method in {"ditto", "company"}
    policy_fields = {"model": model_version, "threshold": settings.threshold,
        "normalization": NORMALIZATION_VERSION, "feature_view": FEATURE_VIEW_VERSION,
        "review_threshold": settings.review_threshold, "blocking": BLOCKING_VERSION, "resolver": "greedy-v2-manual-status",
        "candidate_mode": settings.candidate_mode, "candidate_model": retriever.fingerprint if retriever else None,
        "automatic_merge": not review_only}
    if iso_country:
        policy_fields["country_comparison"] = country_fingerprint()
    policy = digest(policy_fields)
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
        candidates = generate_candidates(country_candidate_view(records) if iso_country else records, retriever=retriever)
        if iso_country:
            candidates = [{**c, "rules": sorted(set(c["rules"]) | {"iso_country_comparison"})} for c in candidates]
        edges = score(records, candidates)
    if review_only:
        edges = [{**edge, "auto_merge": False} for edge in edges]
    candidate = store.prepare_revision(edges, policy_version=policy, threshold=settings.threshold,
        review_threshold=settings.review_threshold, expected_input_hash=digest(raw), verify_full=settings.verify_full,
        force_full=review_only, force_full_reason=("global_candidate_index" if retriever else
            "iso_country_review_candidates" if iso_country else "company_review_candidates" if settings.method == "company"
            else "neural_review_candidates"),
        basis_guard=basis_guard, commit_hook=commit_hook)
    return {**candidate, "candidate_pairs": len(edges), "records": len(records), "method": settings.method,
            "score_refresh": refresh, "candidate_mode": settings.candidate_mode,
            "automatic_merge": not review_only}

