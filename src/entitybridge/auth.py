"""Optional JWT resource-server boundary for one administrator-configured workspace.

This verifies already-issued bearer tokens. It does not implement OAuth login,
refresh tokens, session revocation, provisioning, or browser SSO redirects.
"""

import math
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt

_ROLES = frozenset({"viewer", "reviewer", "admin"})


class AuthenticationError(ValueError):
    """Uniform safe error for all token, claim, key and network failures."""

    def __init__(self):
        super().__init__("Invalid bearer token")


def _url(value, *, allow_loopback_http, issuer=False):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("OIDC URLs must be absolute HTTPS URLs")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("OIDC URL is invalid") from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment
            or (issuer and parsed.query) or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("OIDC URLs cannot contain credentials or fragments; issuer cannot contain a query")
    if parsed.scheme == "https":
        return
    if (allow_loopback_http and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}):
        return
    raise ValueError("OIDC requires HTTPS; HTTP is only allowed for an explicit loopback test")


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    jwks_url: str
    audience: str
    workspace: str
    timeout_seconds: float = 3.0
    cache_seconds: float = 300.0
    refresh_cooldown_seconds: float = 30.0
    leeway_seconds: float = 0.0
    allow_loopback_http: bool = False

    def __post_init__(self):
        if type(self.allow_loopback_http) is not bool:
            raise ValueError("allow_loopback_http must be a boolean")
        _url(self.issuer, allow_loopback_http=self.allow_loopback_http, issuer=True)
        _url(self.jwks_url, allow_loopback_http=self.allow_loopback_http)
        for name in ("audience", "workspace"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError("OIDC audience and workspace must contain 1 to 200 characters")
        for name, maximum, zero_allowed in (("timeout_seconds", 30, False), ("cache_seconds", 3600, False),
                                            ("refresh_cooldown_seconds", 300, True), ("leeway_seconds", 60, True)):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value) or value > maximum
                    or value < 0 or (value == 0 and not zero_allowed)):
                raise ValueError(f"Invalid OIDC {name}")


class OidcVerifier:
    def __init__(self, config: OidcConfig):
        if not isinstance(config, OidcConfig):
            raise TypeError("OIDC verifier requires an OidcConfig")
        self.config = config
        # Do not enable PyJWT's per-key LRU: it has no TTL and would retain removed
        # keys after the JWKS cache expires. 2.14 also rejects HTTP redirects.
        self._jwks = jwt.PyJWKClient(config.jwks_url, cache_keys=False, cache_jwk_set=True,
            lifespan=config.cache_seconds, timeout=config.timeout_seconds,
            cooldown_duration=config.refresh_cooldown_seconds)

    def verify(self, token):
        try:
            if not isinstance(token, str) or not token or len(token) > 16384 or not token.isascii():
                raise AuthenticationError()
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(kid, str) or not kid or len(kid) > 200
                    or "crit" in header or header.get("b64") is False):
                raise AuthenticationError()
            # The only token-supplied selector is a key ID within the configured
            # JWKS. jku/x5u/jwk, tenant headers and request paths cannot select URLs.
            key = self._jwks.get_signing_key(kid)
            if key.algorithm_name != "RS256" or key.key_type != "RSA":
                raise AuthenticationError()
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.config.issuer,
                audience=self.config.audience, leeway=self.config.leeway_seconds,
                options={"require": ["exp", "sub", "iss", "aud"], "enforce_minimum_key_length": True})
            # PyJWT checks temporal validity; constrain NumericDate types as well.
            for name in ("exp", "nbf", "iat"):
                if name in claims and (type(claims[name]) not in (int, float) or not math.isfinite(claims[name])):
                    raise AuthenticationError()
            subject = claims["sub"]
            if not subject.strip() or len(subject) > 100 or any(ord(char) < 32 for char in subject):
                raise AuthenticationError()
            roles = claims.get("entitybridge_roles")
            role = roles.get(self.config.workspace) if isinstance(roles, dict) else None
            if not isinstance(role, str) or role not in _ROLES:
                raise AuthenticationError()
            return subject, role
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
            # Exception details can contain tokens, claims, keys or network URLs.
            raise AuthenticationError() from None
