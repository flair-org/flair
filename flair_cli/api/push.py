"""Push API operations: Push Manifest Handshake and Finalization."""
from typing import Dict, Any
from .utils import _base_url, _client_with_auth


def push_manifest(repo_hash: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Submit push manifest to negotiate missing commits and allocate upload sessions.
    
    Args:
        repo_hash: Repository hash
        manifest: Push manifest payload
        
    Returns:
        Negotiation response with missingCommits and session tokens
    """
    try:
        import sys
        _c_auth = getattr(sys.modules.get("flair_cli.cli.push"), "_client_with_auth", _client_with_auth)
    except Exception:
        _c_auth = _client_with_auth

    with _c_auth() as client:
        r = client.post(f"{_base_url()}/api/repo/hash/{repo_hash}/push/manifest", json=manifest)
        r.raise_for_status()
        res_data = r.json()
        return res_data.get("data", res_data)


def push_finalize(repo_hash: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Finalize push session and receive authoritative commit hash reconciliation map.
    
    Args:
        repo_hash: Repository hash
        payload: Finalization payload
        
    Returns:
        Finalization response with commitHashMap and branchHead
    """
    try:
        import sys
        _c_auth = getattr(sys.modules.get("flair_cli.cli.push"), "_client_with_auth", _client_with_auth)
    except Exception:
        _c_auth = _client_with_auth

    with _c_auth() as client:
        r = client.post(f"{_base_url()}/api/repo/hash/{repo_hash}/push/finalize", json=payload)
        r.raise_for_status()
        res_data = r.json()
        return res_data.get("data", res_data)
