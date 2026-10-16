"""Reversible playlist filenames and library-local owner aliases."""

import re
from urllib.parse import quote, unquote

from beets import ui


def encode_name(name):
    if not isinstance(name, str) or not name:
        raise ui.UserError("AudioStation playlist has no name")
    encoded = quote(name, safe="-_ ").replace(".", "%2E")
    encoded = encoded.rstrip(" ") + "%20" * (len(encoded) - len(encoded.rstrip(" ")))
    if encoded.startswith(("G_", "P_")):
        encoded = f"%{ord(encoded[0]):02X}" + encoded[1:]
    return encoded


def decode_name(encoded):
    try:
        name = unquote(encoded, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise ui.UserError("Invalid encoded playlist name") from None
    if encode_name(name) != encoded:
        raise ui.UserError("Invalid encoded playlist name")
    return name


class OwnerAliases:
    """Store only owner identities, never remote playlist IDs."""

    def __init__(self, lib, pretend=False):
        self.lib = lib
        # In pretend mode, new aliases are only allocated in memory.
        self.pretend = pretend
        self.pending = {}  # owner -> alias
        with lib.transaction() as tx:
            tx.mutate(
                "CREATE TABLE IF NOT EXISTS synologysync_owner_aliases "
                "(alias TEXT PRIMARY KEY, owner TEXT NOT NULL UNIQUE)"
            )

    def alias(self, owner):
        if not isinstance(owner, str) or not owner:
            raise ui.UserError("AudioStation playlist has no owner")
        with self.lib.transaction() as tx:
            rows = tx.query(
                "SELECT alias FROM synologysync_owner_aliases WHERE owner = ?", (owner,)
            )
            if rows:
                alias = rows[0]["alias"]
                if not re.fullmatch(r"[A-Za-z0-9-]{1,8}", alias):
                    raise ui.UserError(f"Invalid stored playlist owner alias: {alias}")
                return alias
            if owner in self.pending:
                return self.pending[owner]
            stem = re.sub(r"[^A-Za-z0-9-]", "-", owner).strip("-") or "owner"
            candidate = stem[:8]
            number = 0
            while True:
                if self.pretend:
                    taken = candidate in self.pending.values() or tx.query(
                        "SELECT 1 FROM synologysync_owner_aliases WHERE alias = ?",
                        (candidate,),
                    )
                    if not taken:
                        self.pending[owner] = candidate
                        return candidate
                else:
                    tx.mutate(
                        "INSERT OR IGNORE INTO synologysync_owner_aliases "
                        "(alias, owner) VALUES (?, ?)",
                        (candidate, owner),
                    )
                    rows = tx.query(
                        "SELECT alias FROM synologysync_owner_aliases WHERE owner = ?",
                        (owner,),
                    )
                    if rows:
                        alias = rows[0]["alias"]
                        if not re.fullmatch(r"[A-Za-z0-9-]{1,8}", alias):
                            raise ui.UserError(f"Invalid stored playlist owner alias: {alias}")
                        return alias
                number += 1
                suffix = str(number)
                if len(suffix) >= 8:
                    raise ui.UserError("Owner alias namespace exhausted")
                candidate = stem[:8 - len(suffix)] + suffix

    def owner(self, alias):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,8}", alias):
            raise ui.UserError(f"Invalid playlist owner alias: {alias}")
        with self.lib.transaction() as tx:
            rows = tx.query(
                "SELECT owner FROM synologysync_owner_aliases WHERE alias = ?", (alias,)
            )
        if not rows:
            raise ui.UserError(f"Unknown playlist owner alias: {alias}; restore the beets database")
        return rows[0]["owner"]

    def filename(self, identity):
        library, owner, name = identity
        prefix = "G" if library == "shared" else "P"
        alias = "" if library == "shared" and owner is None else self.alias(owner)
        stem = f"{prefix}_{alias}_{encode_name(name)}"
        # Never shorten names: truncation would lose the remote identity.
        if len((stem + ".m3u").encode("utf-8")) > 255:
            raise ui.UserError("Encoded playlist filename exceeds 255 bytes")
        return stem

    def identity(self, stem):
        if not stem.startswith(("G_", "P_")):
            return None
        parts = stem.split("_", 2)
        if len(parts) != 3:
            raise ui.UserError(f"Invalid encoded playlist filename: {stem}")
        prefix, alias, name = parts
        return (
            "shared" if prefix == "G" else "personal",
            None if prefix == "G" and not alias else self.owner(alias),
            decode_name(name),
        )


def playlist_category(playlist):
    library = playlist.get("library")
    playlist_id = str(playlist.get("id", ""))
    if not library:
        for category in ("personal", "shared"):
            if playlist_id.startswith(f"playlist_{category}_"):
                library = category
                break
    if library not in ("personal", "shared"):
        raise ui.UserError("AudioStation playlist has an unknown library")
    return library


def playlist_identity(playlist, username):
    library = playlist_category(playlist)
    additional = playlist.get("additional") or {}
    owner = playlist.get("owner") or (
        additional.get("owner") if isinstance(additional, dict) else None
    )
    if not owner and library == "personal":
        owner = username
    if owner and not isinstance(owner, str):
        raise ui.UserError("AudioStation playlist owner is invalid")
    owner = owner or None
    name = playlist.get("name")
    encode_name(name)
    return library, owner, name


def is_smart(playlist):
    return (
        playlist.get("type") == "smart"
        or playlist.get("is_smart") in (True, 1, "true", "1")
        or "_smart/" in str(playlist.get("id", ""))
    )
