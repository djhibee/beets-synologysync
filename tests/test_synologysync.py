import ast
import io
import json
import os
import ssl
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import confuse

from beets import config, ui
from beets.dbcore import types
from beets.library import Item, Library
from beets.plugins import BeetsPlugin
from beetsplug._synology import AudioStation, SynologyError, _NoRedirect
from beetsplug.playlist import PlaylistPlugin
from beetsplug.synologysync import PathMapping, SynologySyncPlugin, _playlist_name


class RatingsType(types.Type):
    model_type = dict

    def to_sql(self, value):
        return json.dumps(value)

    def parse(self, value):
        return json.loads(value)


class ClientTest(unittest.TestCase):
    def test_requires_https_without_embedded_credentials(self):
        for url in ("http://nas", "******nas", "https://nas/?token=bad"):
            with self.subTest(url=url), self.assertRaises(SynologyError):
                AudioStation(url, "user", "secret")

    def test_credentials_are_posted_and_endpoint_is_discovered(self):
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(json.dumps({"success": True, "data": {
                "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7}
            }}).encode()),
            io.BytesIO(b'{"success":true,"data":{"sid":"session"}}'),
            io.BytesIO(b'{"success":true}'),
        ]
        with patch("beetsplug._synology.build_opener", return_value=opener):
            client = AudioStation("https://nas:5001", "user", "secret")
            client.close()
        login = opener.open.call_args_list[1].args[0]
        self.assertEqual(login.full_url, "https://nas:5001/webapi/auth.cgi")
        self.assertEqual(login.method, "POST")
        self.assertIn(b"passwd=secret", login.data)
        self.assertNotIn("secret", login.full_url)
        self.assertIsNone(client.sid)
        self.assertIn(b"_sid=session", opener.open.call_args_list[2].args[0].data)

    def test_api_path_cannot_escape_origin(self):
        client = AudioStation.__new__(AudioStation)
        for path in ("https://other/api", "//other/api", "../auth.cgi", "auth.cgi?x=1"):
            with self.subTest(path=path), self.assertRaises(SynologyError):
                client._request(path, {})

    def test_pagination(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(side_effect=[
            {"songs": [{"id": "1"}], "total": 2},
            {"songs": [{"id": "2"}], "total": 2},
        ])
        self.assertEqual([song["id"] for song in client.songs()], ["1", "2"])
        self.assertEqual(client.call.call_args.kwargs["offset"], 1)
        self.assertEqual(client.call.call_args.kwargs["additional"], "song_rating")

    def test_playlist_song_pagination_uses_nested_response(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(side_effect=[
            {"playlists": [{"additional": {"songs": [{"id": "1"}], "songs_total": 2}}]},
            {"playlists": [{"additional": {"songs": [{"id": "2"}], "songs_total": 2}}]},
        ])
        self.assertEqual(list(client.playlist_songs("p1")), [{"id": "1"}, {"id": "2"}])
        self.assertEqual(client.call.call_args.kwargs["songs_offset"], 1)
        self.assertEqual(client.call.call_args.kwargs["songs_limit"], 500)

    def test_missing_playlist_membership_is_not_treated_as_empty(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(return_value={"playlists": [{"additional": {}}]})
        with self.assertRaises(SynologyError):
            list(client.playlist_songs("p1"))

    def test_playlist_replacement_preserves_id_and_removes_old_membership(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock()
        client.playlist_songs = Mock(return_value=[{"id": "old1"}, {"id": "old2"}])
        client.save_playlist("favourites", ["new"], "p1")
        client.call.assert_called_once_with(
            "SYNO.AudioStation.Playlist", "updatesongs", id="p1",
            offset=0, limit=2, songs="new",
        )

    def test_playlist_creation_uses_new_id(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(side_effect=[{"id": "new"}, {}])
        client.save_playlist("new playlist", [])
        self.assertEqual(client.call.call_args.kwargs,
                         {"id": "new", "offset": 0, "limit": 0, "songs": ""})

    def test_redirects_are_refused(self):
        with self.assertRaises(SynologyError):
            _NoRedirect().redirect_request(None, None, 307, "", {}, "http://other")

    def test_tls_context_verifies_certificate_and_hostname(self):
        with patch("beetsplug._synology.build_opener") as build:
            build.return_value.open.side_effect = OSError("connection refused")
            with self.assertRaises(SynologyError):
                AudioStation("https://nas", "user", "secret")
        context = build.call_args.args[0]._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class PluginTest(unittest.TestCase):
    def setUp(self):
        config.get()
        config_patch = patch.object(config, "sources", list(config.sources))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.music = os.path.join(self.root, "music")
        self.playlists = os.path.join(self.root, "playlists")
        os.makedirs(self.music)
        os.makedirs(self.playlists)
        config["directory"] = self.music
        config["playlist"]["playlist_dir"] = self.playlists
        config["playlist"]["relative_to"] = "playlist"
        config["playlist"]["forward_slash"] = False
        self.playlist_plugin = PlaylistPlugin()
        self.plugin = SynologySyncPlugin()
        self.plugin.config.set({
            "host": "https://nas:5001",
            "synouser": "alice",
            "beetuser": "",
            "force": "no",
            "ca_file": "",
            "timeout": 30,
            "path_mapping": [{"local": self.music, "syno": "/music"}],
        })
        self.lib = Library(os.path.join(self.root, "library.db"))
        self.addCleanup(self.lib._close)
        self.one = Item(path=os.fsencode(os.path.join(self.music, "one.mp3")), title="one")
        self.two = Item(path=os.fsencode(os.path.join(self.music, "two.mp3")), title="two")
        self.lib.add(self.one)
        self.lib.add(self.two)
        self.client = Mock()
        self.client.songs.return_value = [
            {"id": "1", "path": "/music/one.mp3", "additional": {"song_rating": {"rating": 4}}},
            {"id": "2", "path": "/music/two.mp3", "additional": {"song_rating": {"rating": 0}}},
        ]
        self.client.playlists.return_value = [{"id": "p1", "name": "favourites"}]
        self.client.playlist_songs.return_value = list(self.client.songs.return_value)
        self.client_patch = patch("beetsplug.synologysync.AudioStation", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.password_patch = patch("beetsplug.synologysync.getpass.getpass", return_value="secret")
        self.password = self.password_patch.start()
        self.addCleanup(self.password_patch.stop)
        self.plugins_patch = patch("beetsplug.synologysync.plugins.find_plugins",
                                   return_value=[self.playlist_plugin])
        self.plugins_patch.start()
        self.addCleanup(self.plugins_patch.stop)

    def options(self, command=1, arguments=()):
        return self.plugin.commands()[command].parser.parse_args(list(arguments))[0]

    def test_playlist_command_name_and_options(self):
        commands = self.plugin.commands()
        self.assertEqual([command.name for command in commands],
                         ["syncRatings", "syncPlaylist"])
        command = commands[1]
        self.assertEqual(command.func, self.plugin.sync_playlists)
        opts, args = command.parser.parse_args(
            ["--dest=syno", "-p", "favourites", "--conflict=merge", "title:one"]
        )
        self.assertEqual(opts.dest, "syno")
        self.assertEqual(opts.playlist, "favourites")
        self.assertEqual(opts.conflict, "merge")
        self.assertEqual(args, ["title:one"])

    def test_username_fallback_precedence(self):
        opts = self.options()
        self.plugin.config["synouser"] = ""
        cases = [
            ({}, "defaultUser"),
            ({"beets_users": ["first", "second"]}, "first"),
            ({"default_user": "default", "beets_users": ["first"]}, "default"),
            ({"autorate": {"user": "auto"}, "default_user": "default",
              "beets_users": ["first"]}, "auto"),
            ({"autorate": {}, "beets_users": ["first"]}, "first"),
            ({"autorate": {"user": ""}, "default_user": "",
              "beets_users": ["first"]}, "first"),
            ({"beets_users": []}, "defaultUser"),
            ({"beets_users": ["", "second"]}, "defaultUser"),
        ]
        for settings, expected in cases:
            with self.subTest(settings=settings):
                isolated = confuse.Configuration("synologysync-test", read=False)
                isolated["userrating"].set(settings)
                with patch("beetsplug.synologysync.config", isolated):
                    self.assertEqual(self.plugin._username(opts), expected)

    def test_explicit_synology_username_overrides_userrating(self):
        config["userrating"].set({"autorate": {"user": "auto"}})
        self.assertEqual(self.plugin._username(self.options()), "alice")
        self.assertEqual(
            self.plugin._username(self.options(arguments=("--synouser=cli",))), "cli"
        )
        self.plugin.config["synouser"] = ""
        self.assertEqual(
            self.plugin._username(self.options(arguments=("--synouser=cli",))), "cli"
        )

    def test_playlist_connection_uses_fallback_username(self):
        self.plugin.config["synouser"] = ""
        config["userrating"].set({"autorate": {"user": "auto"}})
        self.plugin.sync_playlists(self.lib, self.options(), ["title:one"])
        self.assertEqual(self.client_patch.target.AudioStation.call_args.args[1], "auto")

    def test_mapping_uses_component_boundaries_and_longest_prefix(self):
        mapping = PathMapping([
            {"local": self.music, "syno": "/music"},
            {"local": self.music + "/special", "syno": "/personal"},
        ])
        self.assertEqual(mapping.to_syno(self.music + "/special/a.mp3"), "/personal/a.mp3")
        self.assertIsNone(mapping.to_syno(self.music + "-other/a.mp3"))
        self.assertIsNone(mapping.to_syno(self.music + "/../outside.mp3"))

    def test_playlist_import_obeys_query_and_relative_paths(self):
        self.plugin.sync_playlists(self.lib, self.options(), ["title:one"])
        path = os.path.join(self.playlists, "favourites.m3u")
        self.assertEqual(self.plugin._read_playlist(path, self.playlists), [os.fsdecode(self.one.path)])
        self.client.close.assert_called_once()
        self.password.assert_called_once()

    def test_playlist_export_query_and_name_filter(self):
        path = os.path.join(self.playlists, "favourites.m3u")
        self.plugin._write_playlist(path, self.playlists,
                                    [os.fsdecode(self.two.path), os.fsdecode(self.one.path)])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "-p", "favourites", "--force")),
            ["title:one"],
        )
        self.client.save_playlist.assert_called_once_with("favourites", ["1"], "p1")

    def test_playlist_merge_preserves_order_and_removes_duplicates(self):
        path = os.path.join(self.playlists, "favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.two.path)])
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--conflict=merge",)), [])
        self.assertEqual(self.plugin._read_playlist(path, self.playlists),
                         [os.fsdecode(self.two.path), os.fsdecode(self.one.path)])

    def test_playlist_skip_does_not_write(self):
        path = os.path.join(self.playlists, "favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.two.path)])
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--conflict=skip",)), [])
        self.assertEqual(self.plugin._read_playlist(path, self.playlists), [os.fsdecode(self.two.path)])

    def test_playlist_skip_does_not_fetch_incoming_membership(self):
        path = os.path.join(self.playlists, "favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.two.path)])
        self.client.playlist_songs.side_effect = SynologyError("Unreadable playlist")
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--conflict=skip",)), [])
        self.client.playlist_songs.assert_not_called()

    def test_export_skip_does_not_read_or_map_source(self):
        path = os.path.join(self.playlists, "favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.one.path)])
        self.client.songs.return_value = []
        with patch.object(self.plugin, "_read_playlist", side_effect=OSError("Unreadable")):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--conflict=skip")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_interactive_export_skip_does_not_map_source(self):
        path = os.path.join(self.playlists, "favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.one.path)])
        self.client.songs.return_value = []
        with patch("beetsplug.synologysync.ui.input_options", return_value="s"):
            self.plugin.sync_playlists(self.lib, self.options(arguments=("--dest=syno",)), [])
        self.client.save_playlist.assert_not_called()

    def test_no_matches_does_not_connect_or_overwrite(self):
        self.plugin.sync_playlists(self.lib, self.options(), ["title:missing"])
        self.password.assert_not_called()

    def test_playlist_names_cannot_escape_directory(self):
        for name in ("../outside", "/tmp/outside", "bad\nname", "..", "windows\\path"):
            with self.subTest(name=name), self.assertRaises(ui.UserError):
                _playlist_name(name)

    def test_playlist_symlinks_cannot_escape_directory(self):
        os.symlink(os.path.join(self.root, "outside"), os.path.join(self.playlists, "bad.m3u"))
        with self.assertRaises(ui.UserError):
            self.plugin._local_playlists(self.playlists)

    def test_force_scopes(self):
        opts = SimpleNamespace(force=False)
        self.plugin.config["force"] = "rating"
        self.assertTrue(self.plugin._force(opts, "rating"))
        self.assertFalse(self.plugin._force(opts, "playlist"))
        self.plugin.config["force"] = True
        self.assertTrue(self.plugin._force(opts, "playlist"))

    def prepare_ratings(self):
        # Model the public per-user userrating API without an optional dependency.
        ratings_module = ModuleType("beetsplug.userrating.ratings")
        ratings_module.as_ratings = lambda value: (
            dict(value) if isinstance(value, dict) else ast.literal_eval(value or "{}")
        )
        ratings_module.validate_rating = lambda value: value
        ratings_module.validate_user = lambda value: value
        module_patch = patch.dict("sys.modules", {
            "beetsplug.userrating": ModuleType("beetsplug.userrating"),
            "beetsplug.userrating.ratings": ratings_module,
        })
        module_patch.start()
        self.addCleanup(module_patch.stop)
        type_patch = patch.dict(Item._types, {"userrating": RatingsType()})
        type_patch.start()
        self.addCleanup(type_patch.stop)
        self.plugins_patch.stop()
        self.plugins_patch = patch("beetsplug.synologysync.plugins.find_plugins",
                                  return_value=[BeetsPlugin(name="userrating")])
        self.plugins_patch.start()
        self.addCleanup(self.plugins_patch.stop)
        return ratings_module.as_ratings

    def test_rating_import_preserves_other_users_and_query(self):
        parse = self.prepare_ratings()
        self.one["userrating"] = {"other": 3}
        self.one.store()
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.one.load()
        self.two.load()
        self.assertEqual(parse(self.one.get("userrating")), {"other": 3, "beets-alice": 8})
        self.assertEqual(parse(self.two.get("userrating")), {})
        self.client.close.assert_called_once()

    def test_rating_import_uses_fallback_username(self):
        parse = self.prepare_ratings()
        self.plugin.config["synouser"] = ""
        config["userrating"].set({"default_user": "fallback"})
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.one.load()
        self.assertEqual(parse(self.one.get("userrating")), {"beets-fallback": 8})
        self.assertEqual(self.client_patch.target.AudioStation.call_args.args[1], "fallback")

    def test_rating_export_uses_selected_user_and_rounds_half_stars_up(self):
        self.prepare_ratings()
        self.one["userrating"] = {"beets-bob": 7, "other": 10}
        self.one.store()
        self.client.songs.return_value[0]["additional"]["song_rating"]["rating"] = 2
        self.plugin.sync_ratings(
            self.lib, self.options(0, ("--dest=syno", "--beetuser=bob", "--force")), []
        )
        self.client.set_rating.assert_called_once_with("1", 4)

    def test_existing_rating_keep_and_force(self):
        parse = self.prepare_ratings()
        self.one["userrating"] = {"beets-alice": 0}
        self.one.store()
        with patch("beetsplug.synologysync.ui.input_options", return_value="k") as prompt:
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
            prompt.assert_called_once()
        self.one.load()
        self.assertEqual(parse(self.one.get("userrating"))["beets-alice"], 0)
        with patch("beetsplug.synologysync.ui.input_options") as prompt:
            self.plugin.sync_ratings(self.lib, self.options(0, ("--force",)), ["title:one"])
            prompt.assert_not_called()
        self.one.load()
        self.assertEqual(parse(self.one.get("userrating"))["beets-alice"], 8)

    def test_zero_rating_is_imported(self):
        parse = self.prepare_ratings()
        self.plugin.sync_ratings(self.lib, self.options(0, ("--force",)), ["title:two"])
        self.two.load()
        self.assertEqual(parse(self.two.get("userrating")), {"beets-alice": 0})

    def test_logout_when_sync_fails(self):
        self.prepare_ratings()
        self.client.songs.side_effect = SynologyError("API error")
        with self.assertRaises(ui.UserError):
            self.plugin.sync_ratings(self.lib, self.options(0), [])
        self.client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
