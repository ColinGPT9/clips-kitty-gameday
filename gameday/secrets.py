"""Secrets kept in the database, encrypted with Fernet.

The key is GAMEDAY_SECRET_KEY when it is set, otherwise DATA_DIR/secret.key,
made on first start (mode 0600). What is kept here: the Meta key, this app's
device id and secret for the relay, and the phone alert topic (notify.py).
Values never leave this module in a log line or an answer; a value that no
longer decrypts (the key changed) is treated as not saved.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging

from cryptography.fernet import Fernet, InvalidToken

from gameday.config import SECRET_KEY_FILE, Config, write_private

log = logging.getLogger("gameday")

META_KEY = "meta_api_key"


def _as_fernet_key(text: str) -> bytes:
    """GAMEDAY_SECRET_KEY as a Fernet key: used as it is when it is one,
    otherwise taken as a passphrase and stretched to one."""
    raw = text.strip().encode()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except (binascii.Error, ValueError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(b"gameday-secret-key:" + raw).digest())


def load_key(config: Config) -> bytes:
    if config.secret_key:
        return _as_fernet_key(config.secret_key)
    path = config.data_dir / SECRET_KEY_FILE
    if not path.exists():
        config.data_dir.mkdir(parents=True, exist_ok=True)
        if write_private(path, Fernet.generate_key().decode() + "\n"):
            log.info("Made a new key for stored secrets in %s.", path.resolve())
    return _as_fernet_key(path.read_text(encoding="utf-8"))


class Secrets:
    """Named secrets, each a Fernet token in the store's secrets table."""

    def __init__(self, store, key: bytes):
        self._store = store
        self._fernet = Fernet(key)

    def get(self, name: str) -> str | None:
        token = self._store.secret_token(name)
        if token is None:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            log.warning("The saved %s can't be read with this secret key; it is treated as not saved.", name)
            return None

    def set(self, name: str, value: str) -> None:
        self._store.put_secret_token(name, self._fernet.encrypt(str(value).encode()).decode())

    def delete(self, name: str) -> bool:
        return self._store.delete_secret_token(name)

    def load_json(self, name: str) -> dict | None:
        text = self.get(name)
        if text is None:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def save_json(self, name: str, value: dict) -> None:
        self.set(name, json.dumps(value))


def attach(store, config: Config) -> Secrets:
    """Give `store` its Secrets (store.secrets), keyed from `config`."""
    store.secrets = Secrets(store, load_key(config))
    return store.secrets


def meta_key(config: Config, secrets: Secrets | None) -> tuple[str, bool]:
    """(the Meta key to use, whether it came from META_API_KEY). The
    environment wins when it is set. ("", False) when there is none."""
    if config.meta_api_key:
        return config.meta_api_key, True
    saved = secrets.get(META_KEY) if secrets is not None else None
    return (saved or "").strip(), False
