"""Optional single-user OAuth overlay for the existing static-token MCP server.

Keeping the overlay here limits the upstream-facing integration to a few calls
in ``server.py`` and ``tool_contract.py``. Static-only and stdio deployments
retain their original behavior when ``QUANTDINGER_MCP_AUTH_MODE`` is not
``hybrid``.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError, PyJWTError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.types import CallToolResult, TextContent


READ_SCOPE = "quantdinger.read"
WRITE_SCOPE = "quantdinger.write"
TRADE_SCOPE = "quantdinger.trade"
STATIC_SCOPES = ("mcp", READ_SCOPE, WRITE_SCOPE, TRADE_SCOPE)
TRADE_TOOLS = frozenset(
    {
        "stop_strategy",
        "place_quick_order",
        "emergency_stop_trading",
        "cancel_open_paper_orders",
    }
)


def hybrid_auth_enabled() -> bool:
    return (os.environ.get("QUANTDINGER_MCP_AUTH_MODE") or "static").strip().lower() == "hybrid"


@dataclass(frozen=True)
class HybridAuthConfig:
    oauth_agent_token: str
    issuer: str
    audience: str
    jwks_url: str
    allowed_subject: str

    @classmethod
    def from_env(cls, *, static_token: str, primary_agent_token: str) -> "HybridAuthConfig":
        names = {
            "oauth_agent_token": "QUANTDINGER_MCP_OAUTH_AGENT_TOKEN",
            "issuer": "QUANTDINGER_MCP_OAUTH_ISSUER",
            "audience": "QUANTDINGER_MCP_OAUTH_AUDIENCE",
            "jwks_url": "QUANTDINGER_MCP_OAUTH_JWKS_URL",
            "allowed_subject": "QUANTDINGER_MCP_OAUTH_ALLOWED_SUBJECT",
        }
        values = {field: (os.environ.get(name) or "").strip() for field, name in names.items()}
        missing = [names[field] for field, value in values.items() if not value]
        if missing:
            _fatal(f"missing hybrid OAuth env vars: {', '.join(missing)}")
        for field in ("issuer", "audience", "jwks_url"):
            if not values[field].lower().startswith("https://"):
                _fatal(f"{names[field]} must use HTTPS")
        if not static_token:
            _fatal("hybrid auth requires QUANTDINGER_MCP_AUTH_TOKEN")
        if values["oauth_agent_token"] in {static_token, primary_agent_token}:
            _fatal("OAuth Agent token must differ from the primary Agent and inbound MCP tokens")
        return cls(**values)


def _fatal(message: str) -> None:
    print(f"[quantdinger-mcp] {message}.", file=sys.stderr)
    raise SystemExit(2)


@lru_cache(maxsize=1)
def _config(static_token: str, primary_agent_token: str) -> HybridAuthConfig:
    return HybridAuthConfig.from_env(
        static_token=static_token,
        primary_agent_token=primary_agent_token,
    )


def _normalize_scopes(raw: object) -> list[str]:
    if isinstance(raw, str):
        values: Iterable[object] = raw.split()
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = ()
    return sorted({str(value).strip() for value in values if str(value).strip()})


class OAuthJWTVerifier:
    """Validate asymmetric OAuth access tokens for one issuer and one user."""

    def __init__(self, config: HybridAuthConfig) -> None:
        self.config = config
        self._jwks = PyJWKClient(
            config.jwks_url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )

    def _verify_sync(self, token: str) -> AccessToken | None:
        print(
            "[quantdinger-mcp] _verify_sync entered",
            file=sys.stderr,
            flush=True,
        )

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)

            print(
                f"[quantdinger-mcp] signing key obtained kid={signing_key.key_id!r}",
                file=sys.stderr,
                flush=True,
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.config.audience,
                issuer=self.config.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except (PyJWTError, PyJWKClientError, KeyError, TypeError, ValueError) as exc:
            print(
                "[quantdinger-mcp] JWT VERIFICATION FAILED:",
                type(exc).__name__,
                repr(exc),
                file=sys.stderr,
                flush=True,
            )
            return None

        print(
            "[quantdinger-mcp] OAuth token claims:",
            {
                "iss": claims.get("iss"),
                "aud": claims.get("aud"),
                "sub": claims.get("sub"),
                "scope": claims.get("scope"),
                "scp": claims.get("scp"),
                "permissions": claims.get("permissions"),
                "azp": claims.get("azp"),
            },
            file=sys.stderr,
        )

        subject = str(claims.get("sub") or "").strip()
        if not subject or not secrets.compare_digest(subject, self.config.allowed_subject):
            print(
                f"[quantdinger-mcp] SUBJECT REJECTED: "
                f"actual={subject!r}, expected={self.config.allowed_subject!r}",
                file=sys.stderr,
            )
            return None
        scopes = _normalize_scopes(claims.get("scope") or claims.get("scp"))
        if READ_SCOPE not in scopes:
            print(
                f"[quantdinger-mcp] SCOPE REJECTED: "
                f"actual={scopes!r}, required={READ_SCOPE!r}, "
                f"permissions={claims.get('permissions')!r}",
                file=sys.stderr,
            )
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or "chatgpt-oauth"),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.config.audience,
            subject=subject,
            claims={"auth_mode": "oauth", "iss": self.config.issuer},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await asyncio.to_thread(self._verify_sync, token)


class HybridTokenVerifier:
    def __init__(self, *, static_token: str, oauth: OAuthJWTVerifier) -> None:
        self.static_token = static_token
        self.oauth = oauth

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self.static_token):
            return AccessToken(
                token=token,
                client_id="quantdinger-static-client",
                scopes=list(STATIC_SCOPES),
                claims={"auth_mode": "static"},
            )
        return await self.oauth.verify_token(token)


def build_hybrid_http_auth(
    *, static_token: str, primary_agent_token: str
) -> tuple[AuthSettings, HybridTokenVerifier] | None:
    if not hybrid_auth_enabled():
        return None
    config = _config(static_token, primary_agent_token)
    return (
        AuthSettings(
            issuer_url=config.issuer,
            resource_server_url=config.audience,
            required_scopes=[READ_SCOPE],
        ),
        HybridTokenVerifier(static_token=static_token, oauth=OAuthJWTVerifier(config)),
    )


def apply_oauth_upstream_token(
    kwargs: dict[str, Any], *, static_token: str, primary_agent_token: str
) -> dict[str, Any]:
    if not hybrid_auth_enabled():
        return kwargs
    access_token = get_access_token()
    if (getattr(access_token, "claims", None) or {}).get("auth_mode") != "oauth":
        return kwargs
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {_config(static_token, primary_agent_token).oauth_agent_token}"
    kwargs["headers"] = headers
    return kwargs


def required_scope_for_tool(name: str, write_tools: Mapping[str, bool]) -> str:
    if name in TRADE_TOOLS:
        return TRADE_SCOPE
    if name in write_tools:
        return WRITE_SCOPE
    return READ_SCOPE


def tool_security_meta(name: str, write_tools: Mapping[str, bool]) -> dict[str, Any] | None:
    if not hybrid_auth_enabled():
        return None
    scope = required_scope_for_tool(name, write_tools)
    return {"securitySchemes": [{"type": "oauth2", "scopes": [scope]}]}


def authorize_tool_call(
    name: str, write_tools: Mapping[str, bool]
) -> CallToolResult | None:
    if not hybrid_auth_enabled():
        return None
    access_token = get_access_token()
    if access_token is None:
        return None
    required_scope = required_scope_for_tool(name, write_tools)
    if required_scope in access_token.scopes:
        return None

    audience = (os.environ.get("QUANTDINGER_MCP_OAUTH_AUDIENCE") or "").strip()
    parsed = urlsplit(audience)
    metadata_path = "/.well-known/oauth-protected-resource" + parsed.path.rstrip("/")
    metadata_url = urlunsplit((parsed.scheme, parsed.netloc, metadata_path, "", ""))
    challenge = (
        f'Bearer resource_metadata="{metadata_url}", '
        f'error="insufficient_scope", '
        f'error_description="Required scope: {required_scope}"'
    )
    body = {
        "error": True,
        "status": 403,
        "message": f"Missing required OAuth scope: {required_scope}",
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        structuredContent=body,
        isError=True,
        _meta={"mcp/www_authenticate": [challenge]},
    )
