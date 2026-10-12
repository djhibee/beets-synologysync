"""Verified HTTPS access to the Synology AudioStation WebAPI."""

import json
import posixpath
import ssl
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import getproxies, proxy_bypass

from beets import logging
import urllib3


class SynologyError(Exception):
    pass


def _is_sensitive_key(key):
    k = str(key).lower()
    return (
        k in (
            "_sid",
            "sid",
            "passwd",
            "password",
            "pass",
            "token",
            "session_token",
            "auth_token",
            "secret",
            "key",
            "api_key",
            "apikey",
            "credential",
            "credentials",
        )
        or "passwd" in k
        or "password" in k
        or "token" in k
        or "secret" in k
        or "credential" in k
    )


def _conceal_sensitive(params):
    if isinstance(params, dict):
        concealed = {}
        for k, v in params.items():
            if _is_sensitive_key(k):
                concealed[k] = "***"
            else:
                concealed[k] = _conceal_sensitive(v)
        return concealed
    if isinstance(params, list):
        return [_conceal_sensitive(item) for item in params]
    return params


def _https_url(url, name):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
            or "\\" in url
        ):
            raise ValueError
        parts.port
    except ValueError:
        raise SynologyError(
            f"{name} must be an HTTPS URL without credentials or query"
        ) from None
    return parts


class AudioStation:
    log = logging.getLogger("beets.synologysync")

    def __init__(
        self,
        url,
        username,
        password,
        ca_file=None,
        timeout=30,
        public_url=None,
        log=None,
    ):
        if log is not None:
            self.log = log
        parts = _https_url(url, "url")
        identity = _https_url(public_url, "public_url") if public_url else parts
        if public_url and identity.path not in ("", "/"):
            raise SynologyError("public_url must not contain a path")
        self.base = parts.path.rstrip("/") + "/webapi/"
        self.timeout = timeout
        self.sid = None
        self.apis = {}
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": identity.netloc,
        }
        pool_options = {
            "ssl_context": ssl.create_default_context(cafile=ca_file),
            "cert_reqs": ssl.CERT_REQUIRED,
            "server_hostname": identity.hostname,
            "assert_hostname": identity.hostname,
            "timeout": timeout,
            "retries": False,
        }
        proxy = getproxies().get("https") if not public_url else None
        if proxy and not proxy_bypass(parts.netloc):
            try:
                proxy_url = proxy if "://" in proxy else "http://" + proxy
                proxy_parts = urlsplit(proxy_url)
                proxy_headers = None
                if proxy_parts.username is not None:
                    credentials = (
                        unquote(proxy_parts.username) + ":"
                        + unquote(proxy_parts.password or "")
                    )
                    proxy_headers = urllib3.make_headers(
                        proxy_basic_auth=credentials, proxy_basic_auth_encoding="utf-8"
                    )
                manager = urllib3.ProxyManager(
                    proxy_url,
                    proxy_headers=proxy_headers,
                    proxy_ssl_context=ssl.create_default_context(),
                    **pool_options,
                )
                self.pool = manager.connection_from_host(
                    parts.hostname, port=parts.port or 443, scheme="https"
                )
            except (urllib3.exceptions.HTTPError, ValueError):
                raise SynologyError("AudioStation HTTPS proxy configuration failed") from None
        else:
            self.pool = urllib3.HTTPSConnectionPool(
                parts.hostname, port=parts.port or 443, **pool_options
            )
        try:
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
        except Exception:
            self.pool.close()
            raise

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
        api = params.get("api", path) if isinstance(params, dict) else path
        method = params.get("method", "") if isinstance(params, dict) else ""
        concealed = _conceal_sensitive(params)
        if method:
            self.log.info(
                f"Calling AudioStation API: {api} ({method}) with params: {concealed}"
            )
        else:
            self.log.info(
                f"Calling AudioStation API: {api} with params: {concealed}"
            )
        try:
            response = self.pool.request(
                "POST",
                self.base + path,
                body=urlencode(params).encode("utf-8"),
                headers=self.headers,
                timeout=self.timeout,
                redirect=False,
                retries=False,
            )
            if 300 <= response.status < 400:
                raise SynologyError("AudioStation redirects are not allowed")
            if response.status != 200:
                raise SynologyError("AudioStation HTTPS request failed")
            payload = json.loads(response.data)
        except (urllib3.exceptions.HTTPError, OSError, ValueError):
            if method:
                self.log.warning(
                    f"AudioStation HTTPS request failed for {api} ({method})"
                )
            else:
                self.log.warning(
                    f"AudioStation HTTPS request failed for {api}"
                )
            # Do not expose requests containing credentials or session tokens.
            raise SynologyError("AudioStation HTTPS request failed") from None
        if not isinstance(payload, dict) or not payload.get("success"):
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            if method:
                self.log.warning(
                    f"AudioStation API error ({code}) for {api} ({method})"
                )
            else:
                self.log.warning(
                    f"AudioStation API error ({code}) for {api}"
                )
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
        try:
            if self.sid:
                self.call("SYNO.API.Auth", "logout", session="AudioStation")
        finally:
            self.sid = None
            self.pool.close()

    def _list(self, api, method, key, **params):
        offset = 0
        while True:
            data = self.call(api, method, offset=offset, limit=500, **params)
            entries = data.get(key, [])
            yield from entries
            offset += len(entries)
            if not entries or offset >= data.get("total", offset):
                break

    def songs(self, rating=None, keyword=None):
        filters = {}
        if rating is not None:
            filters = {"song_rating_meq": rating, "song_rating_leq": rating}
        if keyword is not None:
            filters["keyword"] = keyword
            method = "search"
        else:
            method = "list"
        return self._list(
            "SYNO.AudioStation.Song",
            method,
            "songs",
            library="all",
            additional="song_rating",
            **filters,
        )

    def playlists(self, library="all"):
        return self._list(
            "SYNO.AudioStation.Playlist", "list", "playlists", library=library
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

    def save_playlist(self, name, song_ids, playlist_id=None, library="personal"):
        if playlist_id is None:
            playlist_id = self.call(
                "SYNO.AudioStation.Playlist", "create", name=name, library=library
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
