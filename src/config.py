import yaml
from pathlib import Path

_config = None
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "client.yaml"


def load_config(path: str = None) -> dict:
    """Load and cache the client YAML configuration."""
    global _config
    if _config is not None and path is None:
        return _config

    config_path = Path(path) if path else CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f)
    return _config


def save_config(config: dict, path: str = None):
    """Write config back to disk."""
    config_path = Path(path) if path else CONFIG_PATH
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    global _config
    _config = config


def get_config() -> dict:
    """Get the cached config, loading it if needed."""
    if _config is None:
        return load_config()
    return _config
