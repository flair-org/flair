"""
Configuration management for Flair CLI.
Uses YAML file stored at ~/.flair/config.yaml and Pydantic models for validation.

Precedence for config values:
1. Environment variables (FLAIR_*)
2. ~/.flair/config.yaml (if exists)
3. Built-in defaults (localhost for dev)

Debashish Buragohain
"""
from pathlib import Path
from typing import Optional
import yaml
from pydantic import BaseModel
import os

# Allowed base model file extensions
ALLOWED_BASE_MODEL_EXTENSIONS = [
    # The "Blueprint" (Code & Config)
    ".py", ".json", ".yaml",
    # Graph Definitions (Production/Edge)
    ".onnx", ".pb", ".tflite",
    # Full Model Containers (Architecture + Initial Weights)
    ".h5", ".keras", ".pt", ".pth", ".pkl", ".joblib"
]


class FlairConfig(BaseModel):
    api_base_url: Optional[str] = "https://flair-test-backend.onrender.com/"
    auth_url: Optional[str] = "https://flairhub-frontend.netlify.app/signin"
    # Session timeout in hours (default 7 days)
    session_timeout_hours: Optional[int] = 168

def get_config_dir() -> Path:
    """Resolve config directory from FLAIR_CONFIG_DIR / FLAIR_HOME or ~/.flair."""
    env_dir = os.environ.get("FLAIR_CONFIG_DIR") or os.environ.get("FLAIR_HOME")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".flair"

def get_config_path() -> Path:
    """Resolve config file path."""
    return get_config_dir() / "config.yaml"

CONFIG_PATH = get_config_path()
CONFIG_DIR = get_config_dir()


def load_config() -> FlairConfig:
    """Load config from file, falling back to defaults."""
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return FlairConfig(**data)
        except Exception as e:
            # If config file is malformed, warn and use defaults
            print(f"Warning: Could not parse {config_path}: {e}. Using defaults.")
            return FlairConfig()
    
    # Auto-generate on first use
    _generate_default_config()
    return FlairConfig()


def _generate_default_config():
    """Generate default config file on first use."""
    config_dir = get_config_dir()
    config_path = get_config_path()
    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
    
    # Don't overwrite if it already exists
    if config_path.exists():
        return
    
    default_cfg = FlairConfig()
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(default_cfg.model_dump(exclude_none=False), f, default_flow_style=False)
    except Exception as e:
        print(f"Warning: Could not create {config_path}: {e}")


def save_config(cfg: FlairConfig):
    """Save config to file."""
    config_dir = get_config_dir()
    config_path = get_config_path()
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(exclude_none=True), f, default_flow_style=False)