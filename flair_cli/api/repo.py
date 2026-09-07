from typing import Dict, Any
from .utils import _client_with_auth  # Import the shared helper

def create_repo(payload: Dict[str, Any]) -> Dict[str, Any]:
    with _client_with_auth() as client:
        r = client.post("/repo/create", json=payload)
        r.raise_for_status()
        res_data = r.json()
        return res_data.get("data", res_data)

def list_repos() -> Dict[str, Any]:
    with _client_with_auth() as client:
        r = client.get("/repo")
        r.raise_for_status()
        res_data = r.json()
        return res_data.get("data", res_data)


def get_repo(repo_id: str) -> Dict[str, Any]:
    with _client_with_auth() as client:
        r = client.get(f"/repo/hash/{repo_id}")
        r.raise_for_status()
        res_data = r.json()
        return res_data.get("data", res_data)


def get_repo_by_hash(repo_hash: str) -> Dict[str, Any]:
    """Get repository details by hash.
    
    Args:
        repo_hash: Repository hash
    
    Returns:
        Repository data
    """
    with _client_with_auth() as client:
        r = client.get(f"/repo/hash/{repo_hash}")
        r.raise_for_status()
        return r.json().get("data", {})


def clone_repository(repo_hash: str) -> Dict[str, Any]:
    """Clone a repository: fetch repo + branches + latest commits.
    
    Args:
        repo_hash: Repository hash
    
    Returns:
        Clone data with repo info, branches, and latest commits
    """
    with _client_with_auth() as client:
        r = client.get(f"/repo/hash/{repo_hash}/clone")
        r.raise_for_status()
        return r.json().get("data", {})
