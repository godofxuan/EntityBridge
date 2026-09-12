"""Actual RSA JWTs and a loopback JWKS server; this is not an enterprise SSO test."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from entitybridge.auth import AuthenticationError, OidcConfig, OidcVerifier


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def jwk(key, kid):
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
            "kid": kid, "alg": "RS256", "use": "sig"}


@pytest.fixture
def issuer(keys):
    state = {"keys": [jwk(keys[0], "first")], "requests": [], "status": 200, "delay": 0, "body": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            time.sleep(state["delay"])
            body = state["body"] or json.dumps({"keys": state["keys"]}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if state["status"] == 302:
                self.send_header("Location", "/untrusted")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    config = OidcConfig(issuer=base, jwks_url=base + "/keys", audience="entitybridge-api",
                        workspace="workspace-a", allow_loopback_http=True)
    yield state, config
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def token(keys, config, changes=None, *, index=0, kid="first", algorithm="RS256", headers=None):
    now = int(time.time())
    claims = {"iss": config.issuer, "aud": config.audience, "sub": "user-123", "exp": now + 300,
              "nbf": now - 1, "iat": now - 1, "entitybridge_roles": {"workspace-a": "reviewer"}}
    claims.update(changes or {})
    return jwt.encode(claims, keys[index] if algorithm != "none" else None,
                      algorithm=algorithm, headers={"kid": kid, **(headers or {})})


def test_actual_signature_workspace_role_and_jwks_cache(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(config)
    value = token(keys, config)
    assert verifier.verify(value) == ("user-123", "reviewer")
    assert verifier.verify(value) == ("user-123", "reviewer")
    assert state["requests"] == ["/keys"]


@pytest.mark.parametrize("changes", [
    {"iss": "https://other.example"}, {"aud": "another-application"}, {"sub": ""}, {"sub": 12},
    {"exp": 1}, {"nbf": 4102444800}, {"entitybridge_roles": {"workspace-b": "admin"}},
    {"entitybridge_roles": {"workspace-a": "owner"}}, {"entitybridge_roles": {"workspace-a": ["admin"]}},
    {"entitybridge_roles": "admin"}, {"entitybridge_roles": {}},
])
def test_invalid_claims_fail_with_same_public_error(keys, issuer, changes):
    _, config = issuer
    with pytest.raises(AuthenticationError, match="^Invalid bearer token$"):
        OidcVerifier(config).verify(token(keys, config, changes))


@pytest.mark.parametrize("missing", ["exp", "iss", "aud", "sub"])
def test_required_claims_must_be_present(keys, issuer, missing):
    _, config = issuer
    encoded = token(keys, config)
    claims = jwt.decode(encoded, options={"verify_signature": False})
    del claims[missing]
    encoded = jwt.encode(claims, keys[0], algorithm="RS256", headers={"kid": "first"})
    with pytest.raises(AuthenticationError, match="^Invalid bearer token$"):
        OidcVerifier(config).verify(encoded)


def test_unsigned_or_wrong_algorithm_is_rejected_before_jwks_fetch(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(config)
    for value in [token(keys, config, algorithm="none"), token(keys, config, algorithm="RS512"), "broken"]:
        with pytest.raises(AuthenticationError):
            verifier.verify(value)
    assert state["requests"] == []


def test_wrong_signature_does_not_authenticate(keys, issuer):
    _, config = issuer
    with pytest.raises(AuthenticationError):
        OidcVerifier(config).verify(token(keys, config, index=1))


def test_unknown_kid_refreshes_after_cooldown_and_rotated_key_works(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(replace(config, refresh_cooldown_seconds=0))
    verifier.verify(token(keys, config))
    state["keys"] = [jwk(keys[1], "rotated")]
    assert verifier.verify(token(keys, config, index=1, kid="rotated")) == ("user-123", "reviewer")
    assert len(state["requests"]) == 2
    with pytest.raises(AuthenticationError):
        verifier.verify(token(keys, config, kid="unknown"))


def test_unknown_kid_burst_cannot_force_a_fetch_per_token(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(config)
    verifier.verify(token(keys, config))
    for index in range(4):
        with pytest.raises(AuthenticationError):
            verifier.verify(token(keys, config, kid=f"unknown-{index}"))
    assert state["requests"] == ["/keys"]


def test_expired_key_cache_requires_fresh_keys_and_fails_closed_on_outage(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(replace(config, cache_seconds=0.05))
    value = token(keys, config)
    verifier.verify(value)
    state["status"] = 503
    # A still-valid cached key is permitted; an expired cache is never used stale.
    assert verifier.verify(value) == ("user-123", "reviewer")
    time.sleep(0.08)
    with pytest.raises(AuthenticationError):
        verifier.verify(value)


def test_initial_network_failure_and_timeout_fail_closed(keys, issuer):
    state, config = issuer
    state["delay"] = 0.3
    verifier = OidcVerifier(replace(config, timeout_seconds=0.05))
    started = time.monotonic()
    with pytest.raises(AuthenticationError, match="^Invalid bearer token$"):
        verifier.verify(token(keys, config))
    assert time.monotonic() - started < 1.5


def test_token_header_cannot_select_jwks_url_and_redirects_are_not_followed(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(config)
    value = token(keys, config, headers={"jku": "http://127.0.0.1:1/attacker-keys"})
    assert verifier.verify(value) == ("user-123", "reviewer")
    assert state["requests"] == ["/keys"]
    state["status"] = 302
    with pytest.raises(AuthenticationError):
        OidcVerifier(config).verify(value)
    assert "/untrusted" not in state["requests"]


@pytest.mark.parametrize("url", ["http://idp.example/keys", "file:///private/keys", "https://user:pass@idp.example/keys",
                                 "https://idp.example/keys#fragment", "http://localhost.evil.example/keys"])
def test_invalid_configuration_urls_rejected_even_with_test_opt_in(url):
    with pytest.raises(ValueError):
        OidcConfig(issuer="https://idp.example", jwks_url=url, audience="api", workspace="a",
                   allow_loopback_http=True)


def test_loopback_http_requires_explicit_test_flag():
    with pytest.raises(ValueError):
        OidcConfig(issuer="http://127.0.0.1", jwks_url="http://127.0.0.1/keys", audience="api", workspace="a")


def test_concurrent_cache_miss_fetches_jwks_once(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(config)
    value = token(keys, config)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(verifier.verify, [value] * 4)) == [("user-123", "reviewer")] * 4
    assert state["requests"] == ["/keys"]


def test_removed_key_stops_authenticating_after_cache_ttl(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(replace(config, cache_seconds=0.05))
    value = token(keys, config)
    verifier.verify(value)
    state["keys"] = [jwk(keys[1], "replacement")]
    time.sleep(0.08)
    with pytest.raises(AuthenticationError):
        verifier.verify(value)
    assert verifier.verify(token(keys, config, kid="replacement", index=1)) == ("user-123", "reviewer")


def test_failed_unknown_kid_refresh_preserves_valid_cached_known_key(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(replace(config, refresh_cooldown_seconds=0))
    known = token(keys, config)
    verifier.verify(known)
    state["status"] = 503
    with pytest.raises(AuthenticationError):
        verifier.verify(token(keys, config, kid="unknown"))
    assert verifier.verify(known) == ("user-123", "reviewer")


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"keys":[]}', b'{"keys":[{"kty":"RSA","n":"invalid"}]}'])
def test_invalid_jwks_responses_return_uniform_failure(keys, issuer, body):
    state, config = issuer
    state["body"] = body
    with pytest.raises(AuthenticationError, match="^Invalid bearer token$"):
        OidcVerifier(config).verify(token(keys, config))


def test_hmac_and_unrecognized_critical_headers_are_rejected_without_network(keys, issuer):
    state, config = issuer
    verifier = OidcVerifier(config)
    hmac = jwt.encode({"sub": "user-123"}, "a-test-secret-with-at-least-thirty-two-bytes",
                      algorithm="HS256", headers={"kid": "first"})
    critical = token(keys, config, headers={"crit": ["unknown"], "unknown": True})
    for value in [hmac, critical, "a" * 16385]:
        with pytest.raises(AuthenticationError):
            verifier.verify(value)
    assert state["requests"] == []


@pytest.mark.parametrize("role", ["viewer", "reviewer", "admin"])
def test_workspace_is_fixed_and_other_roles_cannot_escalate_it(keys, issuer, role):
    _, config = issuer
    claims = {"workspace": "workspace-b", "entitybridge_roles": {"workspace-a": role, "workspace-b": "admin"}}
    assert OidcVerifier(config).verify(token(keys, config, claims)) == ("user-123", role)


def test_jwk_algorithm_must_match_rs256_allowlist(keys, issuer):
    state, config = issuer
    state["keys"][0]["alg"] = "RS512"
    with pytest.raises(AuthenticationError):
        OidcVerifier(config).verify(token(keys, config))


def test_weak_rsa_key_is_not_accepted(issuer):
    state, config = issuer
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    state["keys"] = [jwk(weak, "weak")]
    with pytest.warns(jwt.warnings.InsecureKeyLengthWarning):
        value = token([weak], config, kid="weak")
    with pytest.raises(AuthenticationError):
        OidcVerifier(config).verify(value)


@pytest.mark.parametrize("setting,value", [("timeout_seconds", 0), ("cache_seconds", float("inf")),
    ("refresh_cooldown_seconds", -1), ("leeway_seconds", 1000), ("workspace", ""), ("audience", "")])
def test_invalid_config_limits_are_rejected(issuer, setting, value):
    _, config = issuer
    with pytest.raises(ValueError):
        replace(config, **{setting: value})
