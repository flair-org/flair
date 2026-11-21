"""SSH setup helpers for Flair CLI."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class SSHSetupMetadata:
    key_path: str
    public_key_path: str
    principal: str
    public_key: str
    created_at: str


def default_ssh_key_path() -> Path:
    """Return the default Flair SSH key path."""
    return Path.home() / ".ssh" / "id_ed25519_flair"


def _compute_fingerprint_from_public_key(public_key_openssh: str) -> str:
    key_parts = public_key_openssh.strip().split()
    if len(key_parts) < 2:
        raise ValueError("Invalid OpenSSH public key format.")

    key_blob = base64.b64decode(key_parts[1].encode("ascii"), validate=True)
    digest = hashlib.sha256(key_blob).digest()
    fingerprint = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"ssh:SHA256:{fingerprint}"


def generate_ssh_keypair(key_path: Path, passphrase: Optional[str] = None, overwrite: bool = False) -> SSHSetupMetadata:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    """Generate an Ed25519 SSH keypair and write it to disk."""
    if key_path.exists() and not overwrite:
        raise FileExistsError(f"SSH key already exists at {key_path}")

    key_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
        if passphrase
        else serialization.NoEncryption()
    )

    private_key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=encryption,
    )
    key_path.write_bytes(private_key_bytes)

    try:
        key_path.chmod(0o600)
    except Exception:
        pass

    public_key_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("utf-8")

    public_key_path = Path(f"{key_path}.pub")
    public_key_path.write_text(public_key_openssh + "\n", encoding="utf-8")

    metadata = SSHSetupMetadata(
        key_path=str(key_path),
        public_key_path=str(public_key_path),
        principal=_compute_fingerprint_from_public_key(public_key_openssh),
        public_key=public_key_openssh,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    return metadata


def detect_shell() -> str:
    """Detect the active shell environment (powershell, cmd, bash)."""
    import os
    import sys

    if os.environ.get("MSYSTEM"):
        return "bash"

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            TH32CS_SNAPPROCESS = 0x00000002

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            h_snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if h_snapshot != -1:
                pe = PROCESSENTRY32()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
                all_procs: dict[int, tuple[int, str]] = {}
                if ctypes.windll.kernel32.Process32First(h_snapshot, ctypes.byref(pe)):
                    while True:
                        all_procs[pe.th32ProcessID] = (
                            pe.th32ParentProcessID,
                            pe.szExeFile.decode("ansi", errors="ignore").lower(),
                        )
                        if not ctypes.windll.kernel32.Process32Next(h_snapshot, ctypes.byref(pe)):
                            break
                ctypes.windll.kernel32.CloseHandle(h_snapshot)

                # Walk up ancestors (handling python venv launcher wrappers)
                curr_pid = os.getppid()
                for _ in range(5):
                    if curr_pid not in all_procs:
                        break
                    parent_pid, exe_name = all_procs[curr_pid]
                    if "cmd.exe" in exe_name:
                        return "cmd"
                    if "powershell" in exe_name or "pwsh" in exe_name:
                        return "powershell"
                    if "bash" in exe_name or "zsh" in exe_name:
                        return "bash"
                    curr_pid = parent_pid
        except Exception:
            pass

        if os.environ.get("SHELL"):
            return "bash"
        return "powershell"

    shell_env = os.environ.get("SHELL", "").lower()
    if "zsh" in shell_env:
        return "zsh"
    return "bash"


def build_activation_script(shell: str, key_path: Path) -> str:
    """Build a shell snippet that starts ssh-agent and loads the Flair SSH key."""
    shell_name = shell.strip().lower()
    normalized_path = str(key_path)

    if shell_name in {"powershell", "pwsh"}:
        return "\n".join([
            "Get-Service ssh-agent | Set-Service -StartupType Automatic",
            "Start-Service ssh-agent",
            f"ssh-add \"{normalized_path}\"",
        ])

    if shell_name in {"cmd", "bat"}:
        return "\n".join([
            "sc config ssh-agent start=auto >nul 2>&1",
            "net start ssh-agent >nul 2>&1",
            f"ssh-add \"{normalized_path}\"",
        ])

    return "\n".join([
        'eval "$(ssh-agent -s)"',
        f"ssh-add '{normalized_path}'",
    ])
