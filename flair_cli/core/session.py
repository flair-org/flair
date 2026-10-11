"""
Session management: stores session token and wallet address in a file under ~/.flair/session.json
NOTE: We never store private keys.

Session expiration is managed via expires_at timestamp. If the current time exceeds expires_at,
the session is considered invalid and the user must re-authenticate.

Debashish Buragohain
"""
from pathlib import Path
from pydantic import BaseModel
import json
from typing import Optional
from datetime import datetime, timedelta

import os

def get_session_path() -> Path:
    """Resolve session path from FLAIR_CONFIG_DIR / FLAIR_HOME or default ~/.flair/session.json."""
    env_dir = os.environ.get("FLAIR_CONFIG_DIR") or os.environ.get("FLAIR_HOME")
    if env_dir:
        return Path(env_dir) / "session.json"
    return Path.home() / ".flair" / "session.json"

SESSION_PATH = get_session_path()

class Session(BaseModel):
    token: str
    principal: Optional[str]
    # SSH-MIGRATION: wallet_address currently holds the authenticated Solana pubkey from
    # browser login. May be renamed (e.g. signing_identity / ssh_fingerprint) when auth
    # and commit signing move to SSH; push.py reads this as the commit principal.
    wallet_address: Optional[str]
    expires_at: Optional[str]  # ISO 8601 datetime string


def load_session() -> Optional[Session]:
    """Load session from disk. Returns None if session doesn't exist or is expired."""
    session_path = get_session_path()
    if session_path.exists():
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
            session = Session(**data)
            
            # Check if session has expired
            if session.expires_at:
                expires_raw = session.expires_at.replace("Z", "+00:00")
                expires = datetime.fromisoformat(expires_raw)
                now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.utcnow()
                if now > expires:
                    # Session expired, clear it
                    clear_session()
                    return None
            
            return session
        except Exception:
            return None
    return None


def save_session(session: Session):
    """Save session to disk with expiration time."""
    session_path = get_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(session.json(), encoding="utf-8")


def is_session_valid() -> bool:
    """Check if a valid, non-expired session exists."""
    session = load_session()
    return session is not None


def get_valid_token() -> Optional[str]:
    """Get valid token if session exists and hasn't expired. Returns None otherwise."""
    session = load_session()
    return session.token if session else None


def clear_session():
    """Delete session file."""
    session_path = get_session_path()
    if session_path.exists():
        session_path.unlink()
