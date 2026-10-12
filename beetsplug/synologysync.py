"""Synchronize beets with Synology AudioStation (GPL-3.0-only)."""

import getpass
import os
import posixpath
import tempfile
from contextlib import contextmanager

from beets import config, plugins, ui
from beets.plugins import BeetsPlugin

from beetsplug._synology import AudioStation, SynologyError, song_path


class PathMapping:
    def __init__(self, mappings):
        self.mappings = []
        for mapping in mappings:
            local = os.path.abspath(os.path.expanduser(mapping["local"]))
            remote = posixpath.normpath(mapping["syno"])
            self.mappings.append((local, remote))
        if not self.mappings:
            raise ui.UserError("synologysync.path_mapping must not be empty")
        self.mappings.sort(key=lambda entry: len(entry[0]), reverse=True)

    def to_syno(self, path):
        path = os.path.abspath(os.fsdecode(path))
        for local, remote in self.mappings:
            if os.path.commonpath([local, path]) == local:
                suffix = os.path.relpath(path, local).replace(os.sep, "/")
                return posixpath.normpath(posixpath.join(remote, suffix))
        return None


def _unique(entries):
    return list(dict.fromkeys(entries))


def _playlist_name(name):
    if (
        not name
        or name in (".", "..")
        or any(char in name for char in "/\\\r\n\0")
    ):
        raise ui.UserError("Unsafe playlist name")
    return name


class SynologySyncPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "host": "",
                "synouser": "",
                "beetuser": "",
                "path_mapping": [],
                "force": "no",
                "ca_file": "",
                "timeout": 30,
            }
        )

    def commands(self):
        commands = []
        for name, handler in (
            ("syncRatings", self.sync_ratings),
            ("syncPlaylistFromSyno", self.sync_playlists),
        ):
            command = ui.Subcommand(name, help="synchronize with Synology AudioStation")
            command.parser.add_option("--url", help="HTTPS AudioStation base URL")
            command.parser.add_option("--synouser", help="AudioStation login")
            command.parser.add_option("--beetuser", help="userrating username suffix")
            command.parser.add_option(
                "--dest", type="choice", choices=("beet", "syno"), default="beet"
            )
            command.parser.add_option(
                "--force", action="store_true", default=False, help="overwrite conflicts"
            )
            if name == "syncPlaylistFromSyno":
                command.parser.add_option("-p", "--playlist", help="playlist name")
                command.parser.add_option(
                    "--conflict",
                    type="choice",
                    choices=("replace", "skip", "merge"),
                    help="noninteractive playlist conflict policy",
                )
            command.func = handler
            commands.append(command)
        return commands

    def _force(self, opts, kind):
        value = self.config["force"].get()
        if isinstance(value, bool):
            value = "yes" if value else "no"
        if value not in ("no", "rating", "playlist", "yes"):
            raise ui.UserError("synologysync.force must be no, rating, playlist, or yes")
        return opts.force or value in (kind, "yes")

    def _username(self, opts):
        username = opts.synouser or self.config["synouser"].get(str)
        if username:
            return username
        userrating = config["userrating"]
        username = (
            userrating["autorate"]["user"].get("")
            or userrating["default_user"].get("")
        )
        if username:
            return username
        users = userrating["beets_users"].get(None)
        return users[0] if users and users[0] else "defaultUser"

    @contextmanager
    def _connect(self, opts):
        url = opts.url or self.config["host"].get(str)
        username = self._username(opts)
        if not url:
            raise ui.UserError("Set synologysync.host or --url")
        client = None
        try:
            client = AudioStation(
                url,
                username,
                getpass.getpass("AudioStation password: "),
                ca_file=self.config["ca_file"].as_filename()
                if self.config["ca_file"].get(str)
                else None,
                timeout=self.config["timeout"].get(float),
            )
            yield client
        except (SynologyError, OSError, ValueError) as exc:
            raise ui.UserError(str(exc)) from None
        finally:
            if client:
                try:
                    client.close()
                except SynologyError:
                    self._log.warning("Unable to log out of AudioStation")

    def _mapping(self):
        try:
            return PathMapping(self.config["path_mapping"].get())
        except (KeyError, TypeError, ValueError):
            raise ui.UserError(
                "path_mapping must contain local/syno path pairs"
            ) from None

    def _song_index(self, client):
        songs = {}
        for song in client.songs():
            path = song_path(song)
            if path in songs and songs[path]["id"] != song["id"]:
                raise ui.UserError(f"Ambiguous AudioStation song path: {path}")
            songs[path] = song
        return songs

    def _plugin(self, name):
        for plugin in plugins.find_plugins():
            if plugin.name == name:
                return plugin
        raise ui.UserError(f"Enable the beets {name} plugin")

    def sync_ratings(self, lib, opts, args):
        self._plugin("userrating")
        try:
            from beetsplug.userrating.ratings import (
                as_ratings,
                validate_rating,
                validate_user,
            )
        except ImportError:
            raise ui.UserError(
                "Install a beets-userrating version supporting per-user ratings"
            ) from None
        username = (
            opts.beetuser
            or self.config["beetuser"].get(str)
            or self._username(opts)
        )
        try:
            user = validate_user("beets-" + username)
        except ValueError as exc:
            raise ui.UserError(str(exc)) from None
        force = self._force(opts, "rating")
        mapping = self._mapping()
        items = list(lib.items(ui.decargs(args)))
        if not items:
            self._log.info("No items match the query")
            return
        with self._connect(opts) as client:
            songs = self._song_index(client)
            for item in items:
                song = songs.get(mapping.to_syno(item.path))
                if song is None:
                    self._log.warning("No mapped AudioStation song for {}", item.path)
                    continue
                try:
                    ratings = as_ratings(item.get("userrating"))
                    local = validate_rating(ratings.get(user))
                    remote = song["additional"]["song_rating"]["rating"]
                    if isinstance(remote, bool) or int(remote) != float(remote):
                        raise ValueError("Invalid AudioStation rating")
                    remote = int(remote)
                    if not 0 <= remote <= 5:
                        raise ValueError("AudioStation rating is outside 0–5")
                except (KeyError, TypeError, ValueError) as exc:
                    raise ui.UserError(f"Unable to read song rating: {exc}") from None
                if opts.dest == "beet":
                    source, destination = remote * 2, local
                else:
                    if local is None:
                        continue
                    source, destination = (local + 1) // 2, remote
                if source == destination:
                    continue
                if destination is not None and not force:
                    self._log.info("Rating exists for {}: {}", item.path, destination)
                    if ui.input_options(("update", "keep")) == "k":
                        continue
                if opts.dest == "beet":
                    updated = ratings.copy()
                    updated[user] = validate_rating(source)
                    item["userrating"] = updated
                    item.store()
                else:
                    client.set_rating(song["id"], source)
                self._log.info("Synced rating to {}: {}", opts.dest, item.path)

    def _playlist_settings(self):
        self._plugin("playlist")
        directory = config["playlist"]["playlist_dir"].as_filename()
        relative = config["playlist"]["relative_to"].get(str)
        if relative == "library":
            base = config["directory"].as_filename()
        elif relative == "playlist":
            base = directory
        else:
            base = config["playlist"]["relative_to"].as_filename()
        return directory, base

    def _read_playlist(self, path, base):
        with open(path, "r", encoding="utf-8-sig", errors="surrogateescape") as stream:
            return [
                os.path.abspath(os.path.join(base, line.rstrip("\r\n")))
                for line in stream
                if line.rstrip("\r\n") and not line.startswith("#")
            ]

    def _write_playlist(self, path, base, paths):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                errors="surrogateescape",
                dir=os.path.dirname(path),
                delete=False,
            ) as stream:
                temporary = stream.name
                stream.write("#EXTM3U\n")
                for entry in paths:
                    entry = os.path.relpath(entry, base)
                    if config["playlist"]["forward_slash"].get(bool):
                        entry = entry.replace(os.sep, "/")
                    if any(char in entry for char in "\r\n\0") or entry.startswith("#"):
                        raise ui.UserError("A song path cannot be represented in M3U")
                    stream.write(entry + "\n")
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _conflict(self, opts, name, exists):
        if not exists:
            return "replace"
        if opts.conflict:
            return opts.conflict
        if self._force(opts, "playlist"):
            return "replace"
        self._log.info("Playlist already exists: {}", name)
        return {"r": "replace", "s": "skip", "m": "merge"}[
            ui.input_options(("replace", "skip", "merge"))
        ]

    def _local_playlists(self, directory):
        playlists = {}
        if not os.path.isdir(directory):
            return playlists
        for filename in sorted(os.listdir(directory)):
            name, extension = os.path.splitext(filename)
            if extension.lower() not in (".m3u", ".m3u8"):
                continue
            _playlist_name(name)
            path = os.path.join(directory, filename)
            if os.path.commonpath(
                [os.path.realpath(directory), os.path.realpath(path)]
            ) != os.path.realpath(directory):
                raise ui.UserError("Playlist symlink escapes playlist_dir")
            if name in playlists:
                raise ui.UserError(f"Ambiguous local playlist name: {name}")
            playlists[name] = path
        return playlists

    def sync_playlists(self, lib, opts, args):
        self._force(opts, "playlist")
        directory, base = self._playlist_settings()
        mapping = self._mapping()
        items = list(lib.items(ui.decargs(args)))
        selected = {os.path.abspath(os.fsdecode(item.path)) for item in items}
        if args and not selected:
            self._log.info("No items match the query")
            return
        local = self._local_playlists(directory)
        if opts.playlist:
            _playlist_name(opts.playlist)
        with self._connect(opts) as client:
            remote = {}
            for playlist in client.playlists():
                name = playlist["name"]
                if opts.playlist and name != opts.playlist:
                    continue
                _playlist_name(name)
                if name in remote:
                    raise ui.UserError(f"Ambiguous AudioStation playlist name: {name}")
                remote[name] = playlist
            songs = self._song_index(client)
            if opts.dest == "beet":
                self._playlists_to_beets(client, opts, mapping, items, remote, local, directory, base, bool(args))
            else:
                self._playlists_to_syno(client, opts, mapping, selected, remote, local, songs, base, bool(args))

    def _playlists_to_beets(self, client, opts, mapping, items, remote, local, directory, base, queried):
        paths = {}
        for item in items:
            remote_path = mapping.to_syno(item.path)
            if remote_path is not None:
                if remote_path in paths:
                    raise ui.UserError(f"Ambiguous mapped beets path: {remote_path}")
                paths[remote_path] = os.path.abspath(os.fsdecode(item.path))
        for name, playlist in remote.items():
            if name in local and opts.conflict == "skip":
                continue
            entries = []
            for song in client.playlist_songs(playlist["id"]):
                path = paths.get(song_path(song))
                if path:
                    entries.append(path)
                else:
                    self._log.debug("Skipping song outside query/mapping: {}", song_path(song))
            if not entries and queried:
                continue
            path = local.get(name, os.path.join(directory, name + ".m3u"))
            policy = self._conflict(opts, name, name in local)
            if policy == "skip":
                continue
            if policy == "merge" and name in local:
                entries = _unique(self._read_playlist(path, base) + entries)
            self._write_playlist(path, base, entries)
            self._log.info("Synced playlist to beets: {}", name)

    def _playlists_to_syno(self, client, opts, mapping, selected, remote, local, songs, base, queried):
        for name, path in local.items():
            if opts.playlist and name != opts.playlist:
                continue
            existing = remote.get(name)
            if existing and opts.conflict == "skip":
                continue
            entries = [entry for entry in self._read_playlist(path, base) if entry in selected]
            if not entries and queried:
                continue
            policy = self._conflict(opts, name, existing is not None)
            if policy == "skip":
                continue
            ids = []
            for entry in entries:
                song = songs.get(mapping.to_syno(entry))
                if song is None:
                    raise ui.UserError(f"No mapped AudioStation song for {entry}")
                ids.append(song["id"])
            if policy == "merge" and existing:
                ids = _unique(
                    [song["id"] for song in client.playlist_songs(existing["id"])] + ids
                )
            client.save_playlist(name, ids, existing["id"] if existing else None)
            self._log.info("Synced playlist to AudioStation: {}", name)
