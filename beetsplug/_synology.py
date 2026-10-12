"""Verified HTTPS access to the Synology AudioStation WebAPI."""

import json
import posixpath
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


class SynologyError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SynologyError("AudioStation redirects are not allowed")


class AudioStation:
    def __init__(self, url, username, password, ca_file=None, timeout=30):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise SynologyError("url must be an HTTPS URL without credentials or query")
        self.base = url.rstrip("/") + "/webapi/"
        self.timeout = timeout
        self.sid = None
        self.apis = {}
        self.opener = build_opener(
            HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
            _NoRedirect(),
        )
        self.apis = self._request(
            "query.cgi",
            {"api": "SYNO.API.Info", "version": 1, "method": "query", "query": "all"},
        )
        self.sid = self.call(
            "SYNO.API.Auth",
            "login",
            account=username,
            passwd=password,
            session="AudioStation",
            format="sid",
        )["sid"]

    def _request(self, path, params):
        if (
            not isinstance(path, str)
            or path.startswith("/")
            or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or urlsplit(path).scheme
            or urlsplit(path).query
            or urlsplit(path).fragment
        ):
            raise SynologyError("Invalid AudioStation API path")
        request = Request(
            self.base + path,
            data=urlencode(params).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            # Do not expose requests containing credentials or session tokens.
            raise SynologyError("AudioStation HTTPS request failed") from None
        if not isinstance(payload, dict) or not payload.get("success"):
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            raise SynologyError(f"AudioStation API error ({code})")
        return payload.get("data", {})

    def call(self, api, method, **params):
        info = self.apis.get(api)
        if not info:
            raise SynologyError(f"AudioStation API unavailable: {api}")
        params.update(api=api, version=info["maxVersion"], method=method)
        if self.sid:
            params["_sid"] = self.sid
        return self._request(info["path"], params)

    def close(self):
        if self.sid:
            try:
                self.call("SYNO.API.Auth", "logout", session="AudioStation")
            finally:
                self.sid = None

    def _list(self, api, method, key, **params):
        offset = 0
        while True:
            data = self.call(api, method, offset=offset, limit=500, **params)
            entries = data.get(key, [])
            yield from entries
            offset += len(entries)
            if not entries or offset >= data.get("total", offset):
                break

    def songs(self):
        return self._list(
            "SYNO.AudioStation.Song",
            "list",
            "songs",
            library="all",
            additional="song_rating",
        )

    def playlists(self):
        return self._list(
            "SYNO.AudioStation.Playlist", "list", "playlists", library="all"
        )

    def playlist_songs(self, playlist_id):
        offset = 0
        while True:
            data = self.call(
                "SYNO.AudioStation.Playlist",
                "getinfo",
                id=playlist_id,
                library="all",
                additional="songs",
                songs_offset=offset,
                songs_limit=500,
            )
            try:
                additional = data["playlists"][0]["additional"]
                songs = additional["songs"]
            except (KeyError, IndexError, TypeError):
                raise SynologyError("Invalid AudioStation playlist response") from None
            yield from songs
            offset += len(songs)
            total = additional.get("songs_total")
            if not songs or (total is not None and offset >= total):
                break
            if total is None and len(songs) < 500:
                break

    def set_rating(self, song_id, rating):
        self.call("SYNO.AudioStation.Song", "setrating", id=song_id, rating=rating)

    def save_playlist(self, name, song_ids, playlist_id=None):
        if playlist_id is None:
            playlist_id = self.call(
                "SYNO.AudioStation.Playlist", "create", name=name, library="personal"
            )["id"]
            count = 0
        else:
            count = sum(1 for _ in self.playlist_songs(playlist_id))
        self.call(
            "SYNO.AudioStation.Playlist",
            "updatesongs",
            id=playlist_id,
            offset=0,
            limit=count,
            songs=",".join(str(song_id) for song_id in song_ids),
        )


def song_path(song):
    """AudioStation reports paths relative to the music shared folder."""
    path = song.get("path")
    if not isinstance(path, str):
        raise SynologyError("AudioStation song has no path")
    return posixpath.normpath(path)
