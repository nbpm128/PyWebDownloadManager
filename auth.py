import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from settings import settings

security = HTTPBasic(auto_error=False)


def require_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> Optional[str]:
    """Verify HTTP Basic Auth credentials.

    If DM_USERNAME and DM_PASSWORD are not configured, auth is disabled and
    all requests are allowed through. Otherwise credentials must match.
    Returns the authenticated username, or None when auth is disabled.
    """
    auth_enabled = bool(settings.username and settings.password)

    if not auth_enabled:
        return None

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = secrets.compare_digest(
        credentials.username.encode(), settings.username.encode()
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode(), settings.password.encode()
    )

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username