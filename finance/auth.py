import time
import jwt
import requests
from django.conf import settings
from rest_framework import authentication, exceptions

_JWKS = None
_JWKS_FETCHED_AT = 0
_KEY_CACHE: dict = {}
_JWKS_TTL_SECONDS = 60 * 60  # 1 hour cache


def _jwks_url() -> str:
    # Frontend API URL issuer domain + /.well-known/jwks.json is supported by Clerk
    return f"https://{settings.CLERK_ISSUER}/.well-known/jwks.json"


def _get_jwks():
    global _JWKS, _JWKS_FETCHED_AT
    now = int(time.time())
    if _JWKS is None or (now - _JWKS_FETCHED_AT) > _JWKS_TTL_SECONDS:
        _JWKS = requests.get(_jwks_url(), timeout=5).json()
        _JWKS_FETCHED_AT = now
    return _JWKS

def _get_public_key(token: str):
    global _JWKS, _JWKS_FETCHED_AT, _KEY_CACHE
    now = int(time.time())
    
    if _JWKS is None or (now - _JWKS_FETCHED_AT) > _JWKS_TTL_SECONDS:
        _JWKS = requests.get(_jwks_url(), timeout=5).json()
        _JWKS_FETCHED_AT = now
        _KEY_CACHE = {}  # invalidate key cache on refresh

    header = jwt.get_unverified_header(token)
    kid = header.get("kid")

    if kid not in _KEY_CACHE:
        key = next((k for k in _JWKS.get("keys", []) if k.get("kid") == kid), None)
        if not key:
            raise exceptions.AuthenticationFailed("Invalid token (kid not found).")
        _KEY_CACHE[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(key)

    return _KEY_CACHE[kid]


class ClerkUser:
    """Lightweight DRF user object (we store user_id as Clerk 'sub')."""
    def __init__(self, user_id: str):
        self.id = user_id
        self.is_authenticated = True


class ClerkJWTAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None

        token = auth.split(" ", 1)[1].strip()
        if not token:
            return None

        try:
            jwks = _get_jwks()
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
            if not key:
                raise exceptions.AuthenticationFailed("Invalid token (kid not found).")

            public_key = _get_public_key(token)

            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=f"https://{settings.CLERK_ISSUER}",
                audience=getattr(settings, "CLERK_AUDIENCE", None) or None,
                options={"verify_aud": bool(getattr(settings, "CLERK_AUDIENCE", None))},
                leeway=60,
            )

            user_id = payload.get("sub")
            if not user_id:
                raise exceptions.AuthenticationFailed("Invalid token (missing sub).")

            return (ClerkUser(user_id), payload)

        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Token expired.")
        except jwt.InvalidTokenError as e:
            raise exceptions.AuthenticationFailed(f"Invalid token: {e}")
        except Exception as e:
            raise exceptions.AuthenticationFailed(str(e))
