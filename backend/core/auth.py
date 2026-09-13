"""Bearer authentication for the service's write endpoints.

The monolith gates `PUT /api/config` with a session-backed `require_admin` that
resolves a user row from its users database. This service is stateless and has
no user store, so it takes a shared secret instead: the operator sets
TOWER_FINDER_ADMIN_TOKEN and presents it as a bearer token.

An unset token closes the write endpoints rather than opening them. This service
is reachable from the public internet through Cloudflare, so a guard that
disabled itself when the variable was missing would reinstate the exposure it
exists to close, on the first deploy that forgot to set it. That is the opposite
of the monolith's WS_AUTH_TOKEN convention, which fails open because an unset
token there means a local development socket rather than a public write.

Fleet feedback ingest gets its own secret (TOWER_FINDER_FEEDBACK_TOKEN) rather
than reusing the admin one: every node in the fleet has to hold the feedback
token, so a node that is lost or read would otherwise also hand over the ranking
config. Both guards share the compare below so only one of them can be the
constant-time one.
"""

import hmac
import os

from fastapi import HTTPException, Request

ENV_VAR = "TOWER_FINDER_ADMIN_TOKEN"
FEEDBACK_ENV_VAR = "TOWER_FINDER_FEEDBACK_TOKEN"


def _configured_token(env_var: str) -> str:
    # Read per request rather than binding at import. Tests set the variable
    # after this module is first imported, and reading late keeps the guard
    # honest when the process environment is changed under it.
    return os.getenv(env_var, "").strip()


def _require_bearer(request: Request, env_var: str, disabled_detail: str) -> None:
    """Reject anything not presenting the bearer token configured in `env_var`."""
    configured = _configured_token(env_var)
    if not configured:
        raise HTTPException(status_code=503, detail=disabled_detail)

    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    presented = presented.strip()
    # RFC 7235 makes auth-scheme case-insensitive, matching the monolith's node
    # bearer guard, which had to be fixed for exactly this.
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(status_code=401, detail="unauthorized")
    # Compare as bytes. `compare_digest` on str raises TypeError the moment
    # either side is non-ASCII, and headers arrive as latin-1-decoded text, so
    # a single high byte in the header would have been a 500 instead of a 401.
    # Constant-time either way: a plain == leaks the secret a byte at a time.
    if not hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")


async def require_admin(request: Request) -> None:
    """Reject anything not presenting the configured admin bearer token."""
    _require_bearer(request, ENV_VAR, f"Config writes are disabled: {ENV_VAR} is not set")


async def require_feedback_token(request: Request) -> None:
    """Reject anything not presenting the configured fleet-feedback bearer token."""
    _require_bearer(request, FEEDBACK_ENV_VAR, f"Feedback is disabled: {FEEDBACK_ENV_VAR} is not set")
