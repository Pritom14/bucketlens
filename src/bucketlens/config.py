"""Where the bucket is and how to reach it.

Settings come from environment variables (``BUCKETLENS_*``), falling back to the config file
``~/.config/bucketlens/config.env`` (written by ``bucketlens setup``, readable only by you).
Secrets are never printed or logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("BUCKETLENS_CONFIG", Path.home() / ".config" / "bucketlens" / "config.env"))

KEYS = (
    "BUCKETLENS_ENDPOINT",
    "BUCKETLENS_REGION",
    "BUCKETLENS_BUCKET",
    "BUCKETLENS_ACCESS_KEY_ID",
    "BUCKETLENS_SECRET_ACCESS_KEY",
    "BUCKETLENS_R2_ACCOUNT_ID",
    "BUCKETLENS_CF_API_TOKEN",
    "BUCKETLENS_CF_QUEUE",
)


class ConfigError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write KEY=VALUE pairs, keeping other keys already in the file. The file is made private (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_env_file(path)
    current.update({k: v for k, v in values.items() if v is not None})
    old_umask = os.umask(0o077)
    try:
        path.write_text(
            "# bucketlens settings. Contains secrets: never paste or commit this file.\n"
            + "".join(f"{k}={v}\n" for k, v in current.items())
        )
    finally:
        os.umask(old_umask)
    path.chmod(0o600)


@dataclass(frozen=True)
class Config:
    endpoint: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    r2_account_id: str = ""
    cf_api_token: str = ""
    cf_queue: str = ""

    @property
    def endpoint_host(self) -> str:
        return self.endpoint.split("://", 1)[-1].rstrip("/")

    @property
    def use_ssl(self) -> bool:
        return not self.endpoint.startswith("http://")

    def __repr__(self) -> str:  # never show secrets
        return f"Config(endpoint={self.endpoint!r}, bucket={self.bucket!r}, region={self.region!r})"


def load(path: Path | None = None) -> Config:
    values = read_env_file(path or CONFIG_FILE)
    values.update({k: os.environ[k] for k in KEYS if os.environ.get(k)})
    account = values.get("BUCKETLENS_R2_ACCOUNT_ID", "")
    endpoint = values.get("BUCKETLENS_ENDPOINT") or (f"https://{account}.r2.cloudflarestorage.com" if account else "")
    region = values.get("BUCKETLENS_REGION") or ("auto" if account else "us-east-1")
    missing = [
        k
        for k, v in (
            ("BUCKETLENS_ENDPOINT or BUCKETLENS_R2_ACCOUNT_ID", endpoint),
            ("BUCKETLENS_BUCKET", values.get("BUCKETLENS_BUCKET")),
            ("BUCKETLENS_ACCESS_KEY_ID", values.get("BUCKETLENS_ACCESS_KEY_ID")),
            ("BUCKETLENS_SECRET_ACCESS_KEY", values.get("BUCKETLENS_SECRET_ACCESS_KEY")),
        )
        if not v
    ]
    if missing:
        raise ConfigError(
            f"missing settings: {', '.join(missing)}. Run `bucketlens setup` or set them in the environment."
        )
    return Config(
        endpoint=endpoint,
        region=region,
        bucket=values["BUCKETLENS_BUCKET"],
        access_key_id=values["BUCKETLENS_ACCESS_KEY_ID"],
        secret_access_key=values["BUCKETLENS_SECRET_ACCESS_KEY"],
        r2_account_id=account,
        cf_api_token=values.get("BUCKETLENS_CF_API_TOKEN", ""),
        cf_queue=values.get("BUCKETLENS_CF_QUEUE", ""),
    )
