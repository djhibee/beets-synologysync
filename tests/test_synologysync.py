import ast
import base64
import json
import os
import ssl
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

import confuse

from beets import config, ui
from beets.dbcore import types
from beets.library import Item, Library
from beets.plugins import BeetsPlugin
from beetsplug._synology import AudioStation, SynologyError, _conceal_sensitive, _is_sensitive_key
from beetsplug._synology_playlists import OwnerAliases, decode_name, encode_name
from beetsplug.playlist import PlaylistPlugin, PlaylistQuery
from beetsplug.synologysync import PathMapping, SynologySyncPlugin, _playlist_name


class RatingsType(types.Type):
    model_type = dict

    def to_sql(self, value):
        return json.dumps(value)

    def parse(self, value):
        return json.loads(value)


class ClientTest(unittest.TestCase):
    def setUp(self):
        proxy_patch = patch("beetsplug._synology.getproxies", return_value={})
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)

    def test_requires_https_without_embedded_credentials(self):
        for url in ("http://nas", "https://user@nas", "https://nas/?token=bad"):
            with self.subTest(url=url), self.assertRaises(SynologyError):
                AudioStation(url, "user", "secret")

    def test_credentials_are_posted_and_endpoint_is_discovered(self):
        pool = Mock()
        pool.request.side_effect = [
            Mock(status=200, data=json.dumps({"success": True, "data": {
                "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7}
            }}).encode()),
            Mock(status=200, data=b'{"success":true,"data":{"sid":"session"}}'),
            Mock(status=200, data=b'{"success":true}'),
        ]
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool", return_value=pool):
            client = AudioStation("https://nas:5001", "user", "secret")
            client.close()
        login = pool.request.call_args_list[1]
        self.assertEqual(login.args, ("POST", "/webapi/auth.cgi"))
        self.assertIn(b"passwd=secret", login.kwargs["body"])
        self.assertNotIn("secret", login.args[1])
        self.assertEqual(login.kwargs["headers"]["Host"], "nas:5001")
        self.assertFalse(login.kwargs["redirect"])
        self.assertFalse(login.kwargs["retries"])
        self.assertIsNone(client.sid)
        self.assertIn(b"_sid=session", pool.request.call_args_list[2].kwargs["body"])
        pool.close.assert_called_once()

    def test_request_logging_formats_params_with_braces(self):
        pool = Mock()
        pool.request.side_effect = [
            Mock(status=200, data=json.dumps({"success": True, "data": {
                "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7}
            }}).encode()),
            Mock(status=200, data=b'{"success":true,"data":{"sid":"session"}}'),
        ]
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool", return_value=pool), \
                self.assertLogs("beets.synologysync", level="INFO") as logs:
            AudioStation("https://nas:5001", "user", "secret")
        messages = [record.getMessage() for record in logs.records]
        self.assertIn("'api': 'SYNO.API.Info'", messages[0])
        self.assertIn("'passwd': '***'", messages[1])
        self.assertNotIn("secret", "\n".join(messages))

    def test_public_url_separates_connection_and_tls_identity(self):
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool") as build:
            build.return_value.request.side_effect = OSError("connection refused")
            with self.assertRaises(SynologyError):
                AudioStation("https://192.168.1.10:5001", "user", "secret",
                             public_url="https://nas.example.org:443")
        self.assertEqual(build.call_args.args, ("192.168.1.10",))
        self.assertEqual(build.call_args.kwargs["port"], 5001)
        self.assertEqual(build.call_args.kwargs["server_hostname"], "nas.example.org")
        self.assertEqual(build.call_args.kwargs["assert_hostname"], "nas.example.org")
        headers = build.return_value.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Host"], "nas.example.org:443")
        build.return_value.close.assert_called_once()

    def test_public_url_validation(self):
        urls = ("http://nas", "https://user@nas", "https://nas/api",
                "https://nas/?x=1", "https://nas/#fragment", "https://nas:invalid",
                "https://nas\r\nHost: other", "https://nas\\other")
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool") as build:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(SynologyError):
                    AudioStation("https://127.0.0.1", "user", "secret", public_url=url)
            build.assert_not_called()

    def test_default_connection_preserves_https_proxy(self):
        with patch("beetsplug._synology.getproxies",
                   return_value={"https": "http://proxy.example:8080"}), \
             patch("beetsplug._synology.proxy_bypass", return_value=False), \
             patch("beetsplug._synology.urllib3.ProxyManager") as build:
            pool = build.return_value.connection_from_host.return_value
            pool.request.side_effect = OSError("connection refused")
            with self.assertRaises(SynologyError):
                AudioStation("https://nas:5001", "user", "secret")
        self.assertEqual(build.call_args.args, ("http://proxy.example:8080",))
        build.return_value.connection_from_host.assert_called_once_with(
            "nas", port=5001, scheme="https"
        )
        self.assertEqual(build.call_args.kwargs["assert_hostname"], "nas")
        self.assertEqual(build.call_args.kwargs["cert_reqs"], ssl.CERT_REQUIRED)
        pool.close.assert_called_once()

    def test_no_proxy_and_public_url_use_direct_pool(self):
        for public_url, bypass in ((None, True), ("https://nas.example.org", False)):
            with self.subTest(public_url=public_url), \
                 patch("beetsplug._synology.getproxies",
                       return_value={"https": "http://proxy.example:8080"}), \
                 patch("beetsplug._synology.proxy_bypass", return_value=bypass), \
                 patch("beetsplug._synology.urllib3.ProxyManager") as manager, \
                 patch("beetsplug._synology.urllib3.HTTPSConnectionPool") as build:
                build.return_value.request.side_effect = OSError("connection refused")
                with self.assertRaises(SynologyError):
                    AudioStation("https://127.0.0.1", "user", "secret",
                                 public_url=public_url)
                manager.assert_not_called()
                self.assertEqual(build.call_args.args, ("127.0.0.1",))

    def test_proxy_authentication_preserves_utf8_encoding(self):
        proxy_url = "http://" + "user:p%C3%A4ss@proxy.example:8080"
        with patch("beetsplug._synology.getproxies", return_value={"https": proxy_url}), \
             patch("beetsplug._synology.proxy_bypass", return_value=False), \
             patch("beetsplug._synology.urllib3.ProxyManager") as build:
            pool = build.return_value.connection_from_host.return_value
            pool.request.side_effect = OSError("connection refused")
            with self.assertRaises(SynologyError):
                AudioStation("https://nas", "user", "secret")
        header = build.call_args.kwargs["proxy_headers"]["proxy-authorization"]
        self.assertEqual(base64.b64decode(header.split()[1]), "user:päss".encode("utf-8"))

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

    def test_rating_filter_is_exact_and_preserved_across_pages(self):
        for rating in range(6):
            with self.subTest(rating=rating):
                client = AudioStation.__new__(AudioStation)
                client.call = Mock(side_effect=[
                    {"songs": [{"id": "1"}], "total": 2},
                    {"songs": [{"id": "2"}], "total": 2},
                ])
                self.assertEqual(
                    list(client.songs(rating=rating)), [{"id": "1"}, {"id": "2"}]
                )
                for offset, call in enumerate(client.call.call_args_list):
                    self.assertEqual(call.args, ("SYNO.AudioStation.Song", "list"))
                    self.assertEqual(call.kwargs["offset"], offset)
                    self.assertEqual(call.kwargs["song_rating_meq"], rating)
                    self.assertEqual(call.kwargs["song_rating_leq"], rating)
                    self.assertEqual(call.kwargs["additional"], "song_rating")

    def test_title_search_is_preserved_across_pages(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(side_effect=[
            {"songs": [{"id": "1"}], "total": 2},
            {"songs": [{"id": "2"}], "total": 2},
        ])
        self.assertEqual(
            list(client.songs(title="Irreplaceable")), [{"id": "1"}, {"id": "2"}]
        )
        for call in client.call.call_args_list:
            self.assertEqual(call.args, ("SYNO.AudioStation.Song", "search"))
            self.assertEqual(call.kwargs["title"], "Irreplaceable")
            self.assertNotIn("keyword", call.kwargs)

    def test_album_and_artist_search_filters(self):
        for field in ("album", "artist"):
            with self.subTest(field=field):
                client = AudioStation.__new__(AudioStation)
                client.call = Mock(return_value={"songs": [], "total": 0})
                list(client.songs(**{field: "X"}))
                args, kwargs = client.call.call_args
                self.assertEqual(args, ("SYNO.AudioStation.Song", "search"))
                self.assertEqual(kwargs[field], "X")
                self.assertNotIn("title", kwargs)

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

    def test_playlist_library_selection_is_sent_to_api(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(return_value={"playlists": [], "total": 0})
        list(client.playlists(library="shared"))
        self.assertEqual(client.call.call_args.kwargs["library"], "shared")
        list(client.playlists())
        self.assertEqual(client.call.call_args.kwargs["library"], "all")

    def test_shared_creation_does_not_fall_back_on_permission_failure(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(side_effect=SynologyError("permission denied"))
        with self.assertRaises(SynologyError):
            client.save_playlist("shared playlist", ["song"], library="shared")
        client.call.assert_called_once_with(
            "SYNO.AudioStation.Playlist", "create",
            name="shared playlist", library="shared",
        )

    def test_shared_creation_uses_requested_library_and_new_id(self):
        client = AudioStation.__new__(AudioStation)
        client.call = Mock(side_effect=[{"id": "shared_new"}, {}])
        client.save_playlist("shared playlist", ["song"], library="shared")
        self.assertEqual(client.call.call_args_list[0].kwargs["library"], "shared")
        self.assertEqual(client.call.call_args_list[1].kwargs["id"], "shared_new")

    def test_redirects_are_refused(self):
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool") as build:
            build.return_value.request.return_value = Mock(status=307)
            with self.assertRaisesRegex(SynologyError, "redirects are not allowed"):
                AudioStation("https://nas", "user", "secret")
        self.assertEqual(build.return_value.request.call_count, 1)
        self.assertFalse(build.return_value.request.call_args.kwargs["redirect"])

    def test_tls_context_verifies_certificate_and_hostname(self):
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool") as build:
            build.return_value.request.side_effect = OSError("connection refused")
            with self.assertRaises(SynologyError):
                AudioStation("https://nas", "user", "secret")
        context = build.call_args.kwargs["ssl_context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(build.call_args.kwargs["assert_hostname"], "nas")
        self.assertEqual(build.call_args.kwargs["server_hostname"], "nas")

    def test_pool_is_closed_even_if_logout_fails(self):
        client = AudioStation.__new__(AudioStation)
        client.sid = "session"
        client.pool = Mock()
        client.call = Mock(side_effect=SynologyError("logout failed"))
        with self.assertRaises(SynologyError):
            client.close()
        self.assertIsNone(client.sid)
        client.pool.close.assert_called_once()

    def test_transport_errors_do_not_expose_credentials(self):
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool") as build:
            build.return_value.request.side_effect = OSError("passwd=sensitive-value")
            with self.assertRaisesRegex(SynologyError, "^AudioStation HTTPS request failed$"):
                AudioStation("https://nas", "user", "sensitive-value")
        build.return_value.close.assert_called_once()

    def test_api_calls_are_logged_and_sensitive_data_concealed(self):
        pool = Mock()
        pool.request.side_effect = [
            Mock(status=200, data=json.dumps({"success": True, "data": {
                "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7},
                "SYNO.AudioStation.Song": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
            }}).encode()),
            Mock(status=200, data=b'{"success":true,"data":{"sid":"super-secret-token"}}'),
            Mock(status=200, data=b'{"success":true,"data":{"songs":[],"total":0}}'),
            Mock(status=200, data=b'{"success":true}'),
        ]
        log_mock = Mock()
        with patch("beetsplug._synology.urllib3.HTTPSConnectionPool", return_value=pool):
            client = AudioStation("https://nas:5001", "user", "secret-password", log=log_mock)
            list(client.songs())
            client.close()

        info_calls = log_mock.info.call_args_list
        all_logged_text = " ".join(
            call.args[0].format(*call.args[1:]) for call in info_calls
        )

        self.assertNotIn("secret-password", all_logged_text)
        self.assertNotIn("super-secret-token", all_logged_text)

        self.assertIn("Calling AudioStation API: SYNO.API.Info", all_logged_text)
        self.assertIn("Calling AudioStation API: SYNO.API.Auth", all_logged_text)
        self.assertIn("Calling AudioStation API: SYNO.AudioStation.Song", all_logged_text)
        self.assertIn("'passwd': '***'", all_logged_text)
        self.assertIn("'_sid': '***'", all_logged_text)

    def test_sensitive_concealment_helpers(self):
        for key in ("passwd", "password", "my_password", "token", "_sid", "sid", "secret", "credentials"):
            self.assertTrue(_is_sensitive_key(key), f"Expected {key} to be sensitive")
        for key in ("account", "api", "method", "offset", "limit", "song_rating_meq", "id", "songs"):
            self.assertFalse(_is_sensitive_key(key), f"Expected {key} not to be sensitive")

        data = {
            "account": "alice",
            "passwd": "secret-pass",
            "_sid": "token123",
            "nested": {"password": "pass2", "regular": "val"},
            "items": [{"api_key": "key1", "name": "track"}],
        }
        concealed = _conceal_sensitive(data)
        self.assertEqual(concealed["account"], "alice")
        self.assertEqual(concealed["passwd"], "***")
        self.assertEqual(concealed["_sid"], "***")
        self.assertEqual(concealed["nested"]["password"], "***")
        self.assertEqual(concealed["nested"]["regular"], "val")
        self.assertEqual(concealed["items"][0]["api_key"], "***")
        self.assertEqual(concealed["items"][0]["name"], "track")
        self.assertEqual(data["passwd"], "secret-pass")


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
        self.client.playlists.return_value = [
            {"id": "p1", "name": "favourites", "library": "personal",
             "owner": "alice", "type": "normal"}
        ]
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

    def test_public_url_is_passed_to_client_with_cli_connection_override(self):
        self.plugin.config["public_url"] = "https://nas.example.org:5001"
        opts = self.options(arguments=("--url=https://192.168.1.10:5001",))
        self.plugin.sync_playlists(self.lib, opts, ["title:one"])
        call = self.client_patch.target.AudioStation.call_args
        self.assertEqual(call.args[0], "https://192.168.1.10:5001")
        self.assertEqual(call.kwargs["public_url"], "https://nas.example.org:5001")

    def test_playlist_command_name_and_options(self):
        commands = self.plugin.commands()
        self.assertEqual([command.name for command in commands],
                         ["syncSynoRatings", "syncSynoPlaylist"])
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
        path = os.path.join(self.playlists, "P_alice_favourites.m3u")
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
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], "p1", library="personal"
        )

    def test_playlist_import_does_not_fetch_song_library(self):
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.client.songs.assert_not_called()
        self.client.playlist_songs.assert_called_once_with("p1")

    def test_playlist_export_skips_song_library_when_nothing_to_resolve(self):
        path = os.path.join(self.playlists, "favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.one.path)])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--conflict=skip")), []
        )
        self.client.songs.assert_not_called()

    def test_playlist_export_fetches_song_library_once(self):
        for name in ("first", "second"):
            self.plugin._write_playlist(
                os.path.join(self.playlists, name + ".m3u"), self.playlists,
                [os.fsdecode(self.one.path)],
            )
        self.client.playlists.return_value = []
        self.client.save_playlist.reset_mock()
        self.plugin.config["SingleAPICallThreshold"] = 1
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--dest=syno",)), [])
        self.client.songs.assert_called_once_with()
        self.assertEqual(self.client.save_playlist.call_count, 2)

    def test_small_playlist_export_uses_song_search_and_reuses_results(self):
        for name in ("first", "second"):
            self.plugin._write_playlist(
                os.path.join(self.playlists, name + ".m3u"), self.playlists,
                [os.fsdecode(self.one.path), os.fsdecode(self.two.path)],
            )
        self.client.playlists.return_value = []
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--dest=syno",)), [])
        self.assertEqual(self.client.songs.call_args_list, [
            call(rating=None, title="one"), call(rating=None, title="two"),
        ])
        self.client.save_playlist.assert_has_calls([
            call("first", ["1", "2"], None, library="personal"),
            call("second", ["1", "2"], None, library="personal"),
        ])

    def test_playlist_export_search_falls_back_to_album_then_artist(self):
        self.one.title, self.one.album, self.one.artist = "", "Album", "Artist"
        self.one.store()
        self.plugin._write_playlist(
            os.path.join(self.playlists, "first.m3u"), self.playlists,
            [os.fsdecode(self.one.path)],
        )
        self.client.playlists.return_value = []
        self.client.songs.side_effect = lambda **kw: (
            [{"id": "1", "path": "/music/one.mp3"}] if "artist" in kw else []
        )
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--dest=syno",)), [])
        self.assertEqual(self.client.songs.call_args_list, [
            call(rating=None, album="Album"), call(rating=None, artist="Artist"),
        ])
        self.client.save_playlist.assert_called_once_with(
            "first", ["1"], None, library="personal"
        )

    def test_playlist_export_without_search_fields_uses_global_index(self):
        self.one.title = ""
        self.one.store()
        self.plugin._write_playlist(
            os.path.join(self.playlists, "first.m3u"), self.playlists,
            [os.fsdecode(self.one.path)],
        )
        self.client.playlists.return_value = []
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--dest=syno",)), [])
        self.client.songs.assert_called_once_with()

    def test_playlist_export_search_miss_reports_not_found(self):
        self.plugin._write_playlist(
            os.path.join(self.playlists, "first.m3u"), self.playlists,
            [os.fsdecode(self.one.path)],
        )
        self.client.playlists.return_value = []
        self.client.songs.return_value = [{"id": "9", "path": "/other/one.mp3"}]
        with self.assertRaisesRegex(ui.UserError, "not found in AudioStation"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno",)), []
            )
        self.client.save_playlist.assert_not_called()

    def test_playlist_merge_preserves_order_and_removes_duplicates(self):
        OwnerAliases(self.lib).alias("alice")
        path = os.path.join(self.playlists, "P_alice_favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.two.path)])
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--conflict=merge",)), [])
        self.assertEqual(self.plugin._read_playlist(path, self.playlists),
                         [os.fsdecode(self.two.path), os.fsdecode(self.one.path)])

    def test_playlist_skip_does_not_write(self):
        OwnerAliases(self.lib).alias("alice")
        path = os.path.join(self.playlists, "P_alice_favourites.m3u")
        self.plugin._write_playlist(path, self.playlists, [os.fsdecode(self.two.path)])
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--conflict=skip",)), [])
        self.assertEqual(self.plugin._read_playlist(path, self.playlists), [os.fsdecode(self.two.path)])

    def test_playlist_skip_does_not_fetch_incoming_membership(self):
        OwnerAliases(self.lib).alias("alice")
        path = os.path.join(self.playlists, "P_alice_favourites.m3u")
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

    def remote_playlist(self, name="favourites", owner="alice",
                        library="personal", playlist_id="p1", **extra):
        return dict(id=playlist_id, name=name, owner=owner, library=library,
                    type="normal", **extra)

    def local_playlist(self, stem, songs=None):
        path = os.path.join(self.playlists, stem + ".m3u")
        self.plugin._write_playlist(
            path, self.playlists,
            songs if songs is not None else [os.fsdecode(self.one.path)],
        )
        return path

    def test_personal_shared_homonyms_import_and_export_preserve_identity(self):
        self.client.playlists.return_value.append(
            self.remote_playlist(library="shared", playlist_id="g1")
        )
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(sorted(os.listdir(self.playlists)),
                         ["G_alice_favourites.m3u", "P_alice_favourites.m3u"])
        self.client.save_playlist.reset_mock()
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.assertEqual(self.client.save_playlist.call_count, 2)
        self.client.save_playlist.assert_any_call(
            "favourites", ["1", "2"], "g1", library="shared"
        )
        self.client.save_playlist.assert_any_call(
            "favourites", ["1", "2"], "p1", library="personal"
        )

    def test_multiple_shared_owners_keep_separate_flat_files(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="g1"),
            self.remote_playlist(owner="alice", library="shared", playlist_id="g2"),
        ]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(sorted(os.listdir(self.playlists)),
                         ["G_alice_favourites.m3u", "G_bob_favourites.m3u"])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "-p", "G_bob_favourites", "--force")),
            [],
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1", "2"], "g1", library="shared"
        )

    def test_owner_alias_collision_and_unsafe_characters_survive_reopen(self):
        owners = ["longusername-one", "longusername-two", "longuser",
                  "a_b/c\\d", "a-b-c-d", "☃", "alice"]
        aliases = OwnerAliases(self.lib)
        allocated = {owner: aliases.alias(owner) for owner in owners}
        self.assertEqual(len(set(allocated.values())), len(owners))
        self.assertTrue(all(len(alias) <= 8 and "_" not in alias
                            for alias in allocated.values()))
        filename = aliases.filename(("shared", owners[3], "with_under_scores"))
        database = self.lib.path
        self.lib._close()
        self.lib = Library(database)
        self.addCleanup(self.lib._close)
        reopened = OwnerAliases(self.lib)
        for owner, alias in allocated.items():
            self.assertEqual(reopened.alias(owner), alias)
            self.assertEqual(reopened.owner(alias), owner)
        self.assertEqual(reopened.identity(filename),
                         ("shared", owners[3], "with_under_scores"))
        with self.lib.transaction() as tx:
            columns = tx.query("PRAGMA table_info(synologysync_owner_aliases)")
        self.assertEqual([row["name"] for row in columns], ["alias", "owner"])

    def test_owner_alias_concurrent_allocation_is_unique_and_stable(self):
        other_lib = Library(self.lib.path)
        self.addCleanup(other_lib._close)
        aliases = [OwnerAliases(self.lib), OwnerAliases(other_lib)]
        owners = ["longusername-one", "longusername-two"] * 8
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda entry: aliases[entry[0] % 2].alias(entry[1]),
                enumerate(owners),
            ))
        allocated = {owner: alias for owner, alias in zip(owners, results)}
        self.assertEqual(len(set(allocated.values())), 2)
        for owner, alias in zip(owners, results):
            self.assertEqual(alias, allocated[owner])
            self.assertEqual(aliases[0].owner(alias), owner)

    def test_remote_original_names_escape_and_roundtrip(self):
        names = ["with_under_scores", "G_fake_name", "P_fake_name",
                 "../a/b\\c:100%?*|<>", "雪 ☃ #Mix", "trailing. ",
                 "A%2FB", ".", "..", "line\nbreak"]
        for name in names:
            self.assertEqual(decode_name(encode_name(name)), name)
        self.client.playlists.return_value = [
            self.remote_playlist(name=name, playlist_id=str(index))
            for index, name in enumerate(names)
        ]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(len(os.listdir(self.playlists)), len(names))
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        for index, name in enumerate(names):
            self.client.save_playlist.assert_any_call(
                name, ["1", "2"], str(index), library="personal"
            )

    def test_overlong_remote_name_is_rejected_not_truncated(self):
        self.client.playlists.return_value = [
            self.remote_playlist(name="a" * 300)
        ]
        with self.assertRaisesRegex(ui.UserError, "255 bytes"):
            self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(os.listdir(self.playlists), [])

    def test_library_config_and_cli_override(self):
        self.client.playlists.return_value.append(
            self.remote_playlist(library="shared", playlist_id="g1")
        )
        self.plugin.config["library"] = "personal"
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.client.playlists.assert_called_with(library="personal")
        self.assertEqual(os.listdir(self.playlists), ["P_alice_favourites.m3u"])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--library=shared",)), []
        )
        self.client.playlists.assert_called_with(library="shared")
        self.assertIn("G_alice_favourites.m3u", os.listdir(self.playlists))

    def test_invalid_library_configuration_is_rejected_before_login(self):
        for key, value in (("library", "invalid"), ("playlist_library", "all")):
            with self.subTest(key=key):
                old = self.plugin.config[key].get()
                self.plugin.config[key] = value
                with self.assertRaises(ui.UserError):
                    self.plugin.sync_playlists(self.lib, self.options(), [])
                self.plugin.config[key] = old
        self.password.assert_not_called()

    def test_new_shared_creation_uses_creation_config(self):
        self.local_playlist("new_playlist")
        self.plugin.config["playlist_library"] = "shared"
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--library=shared")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "new_playlist", ["1"], None, library="shared"
        )

    def test_new_personal_creation_is_default(self):
        self.local_playlist("new_playlist")
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno",)), []
        )
        self.client.save_playlist.assert_called_once_with(
            "new_playlist", ["1"], None, library="personal"
        )

    def test_creation_outside_selected_library_fails_without_fallback(self):
        self.local_playlist("new_playlist")
        with self.assertRaisesRegex(ui.UserError, "outside --library"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--library=shared")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_smart_playlists_are_not_imported(self):
        smart = self.remote_playlist(library="shared", owner="")
        smart["type"] = "smart"
        self.client.playlists.return_value = [smart]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(os.listdir(self.playlists), [])
        self.client.playlist_songs.assert_not_called()

    def test_smart_existing_export_is_skipped_not_created(self):
        self.local_playlist("favourites")
        self.client.playlists.return_value[0]["type"] = "smart"
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_not_called()
        self.client.playlist_songs.assert_not_called()

    def test_ownerless_shared_smart_export_targets_are_skipped(self):
        smart = self.remote_playlist(owner="", library="shared", playlist_id="g1")
        smart["type"] = "smart"
        self.client.playlists.return_value = [smart]
        stem = OwnerAliases(self.lib).filename(("shared", "bob", "favourites"))
        self.local_playlist(stem)
        self.local_playlist("favourites")
        self.plugin.config["playlist_library"] = "shared"
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--library=shared", "--force")), []
        )
        self.client.save_playlist.assert_not_called()
        self.client.playlist_songs.assert_not_called()

    def test_smart_id_and_boolean_markers_are_skipped(self):
        for marker in ({"id": "playlist_personal_smart/favourites"},
                       {"is_smart": True}, {"is_smart": "true"}):
            with self.subTest(marker=marker):
                playlist = self.remote_playlist()
                playlist.update(marker)
                self.client.playlists.return_value = [playlist]
                self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(os.listdir(self.playlists), [])

    def test_duplicate_remote_identity_is_rejected_even_with_force(self):
        self.client.playlists.return_value.append(self.remote_playlist(playlist_id="p2"))
        self.local_playlist("favourites")
        for dest in ("beet", "syno"):
            with self.subTest(dest=dest), self.assertRaisesRegex(ui.UserError, "Ambiguous"):
                self.plugin.sync_playlists(
                    self.lib, self.options(arguments=(f"--dest={dest}", "--force")), []
                )
        self.client.save_playlist.assert_not_called()

    def test_plain_export_homonyms_only_update_default_identity(self):
        self.client.playlists.return_value.append(
            self.remote_playlist(library="shared", playlist_id="g1")
        )
        self.local_playlist("favourites")
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], "p1", library="personal"
        )

    def test_export_original_name_homonyms_require_exact_stem(self):
        self.client.playlists.return_value.append(
            self.remote_playlist(library="shared", playlist_id="g1")
        )
        self.plugin.sync_playlists(self.lib, self.options(), [])
        with self.assertRaisesRegex(ui.UserError, "Ambiguous playlist selection"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force", "-p", "favourites")), []
            )
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force", "-p", "G_alice_favourites")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1", "2"], "g1", library="shared"
        )

    def test_import_original_name_selects_homonyms_but_exact_stem_selects_one(self):
        self.client.playlists.return_value += [
            self.remote_playlist(library="shared", playlist_id="g1"),
            self.remote_playlist(name="unselected", playlist_id="p2"),
        ]
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("-p", "G_alice_favourites")), []
        )
        self.assertEqual(os.listdir(self.playlists), ["G_alice_favourites.m3u"])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("-p", "favourites", "--force")), []
        )
        self.assertEqual(len(os.listdir(self.playlists)), 2)

    def test_unknown_alias_is_not_replaced_by_remote_owner(self):
        self.local_playlist("P_unknown_favourites")
        with self.assertRaisesRegex(ui.UserError, "Unknown playlist owner alias"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_import_lost_alias_table_cannot_rebind_colliding_owner_with_force_or_skip(self):
        aliases = OwnerAliases(self.lib)
        stem = aliases.filename(("shared", "longusername-one", "favourites"))
        path = self.local_playlist(stem)
        self.assertEqual(stem, "G_longuser_favourites")
        with self.lib.transaction() as tx:
            tx.mutate("DROP TABLE synologysync_owner_aliases")
        self.client.playlists.return_value = [
            self.remote_playlist(
                owner="longusername-two", library="shared", playlist_id="g2"
            )
        ]
        for arguments in (("--force",), ("--conflict=skip",)):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ui.UserError, "Unknown playlist owner alias"):
                    self.plugin.sync_playlists(
                        self.lib, self.options(arguments=arguments), []
                    )
                with self.lib.transaction() as tx:
                    self.assertEqual(
                        tx.query("SELECT alias, owner FROM synologysync_owner_aliases"), []
                    )
                self.assertEqual(
                    self.plugin._read_playlist(path, self.playlists),
                    [os.fsdecode(self.one.path)],
                )
        self.password.assert_not_called()
        with self.assertRaisesRegex(ui.UserError, "Unknown playlist owner alias"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_import_checks_other_category_before_allocating_global_owner_alias(self):
        stem = OwnerAliases(self.lib).filename(
            ("personal", "longusername-one", "favourites")
        )
        self.local_playlist(stem)
        with self.lib.transaction() as tx:
            tx.mutate("DELETE FROM synologysync_owner_aliases")
        self.client.playlists.return_value = [
            self.remote_playlist(
                owner="longusername-two", library="shared", playlist_id="g2"
            )
        ]
        with self.assertRaisesRegex(ui.UserError, "Unknown playlist owner alias"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--library=shared", "--force")), []
            )
        with self.lib.transaction() as tx:
            self.assertEqual(
                tx.query("SELECT alias, owner FROM synologysync_owner_aliases"), []
            )
        self.password.assert_not_called()

    def test_invalid_stored_alias_cannot_create_unsafe_import_filename(self):
        aliases = OwnerAliases(self.lib)
        aliases.alias("alice")
        with self.lib.transaction() as tx:
            tx.mutate(
                "UPDATE synologysync_owner_aliases SET alias = ? WHERE owner = ?",
                ("../bad", "alice"),
            )
        with self.assertRaisesRegex(ui.UserError, "Invalid stored"):
            self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(os.listdir(self.playlists), [])

    def test_malformed_encoded_filenames_fail_before_remote_write(self):
        for stem in ("P_alice", "P_alice_%ZZ", "G__", "P__name", "P_alice_"):
            with self.subTest(stem=stem):
                path = self.local_playlist(stem)
                OwnerAliases(self.lib).alias("alice")
                with self.assertRaises(ui.UserError):
                    self.plugin.sync_playlists(
                        self.lib, self.options(arguments=("--dest=syno", "--force")), []
                    )
                os.unlink(path)
        self.client.save_playlist.assert_not_called()

    def test_stale_foreign_personal_owner_never_creates_as_login(self):
        stem = OwnerAliases(self.lib).filename(("personal", "old_owner", "favourites"))
        self.local_playlist(stem)
        with self.assertRaisesRegex(ui.UserError, "another owner"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_stale_foreign_shared_owner_never_changes_other_owner(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="g1")
        ]
        stem = OwnerAliases(self.lib).filename(("shared", "old_owner", "favourites"))
        self.local_playlist(stem)
        with self.assertRaisesRegex(ui.UserError, "another owner"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_foreign_personal_existing_owner_can_be_updated_by_id(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", playlist_id="bob1")
        ]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1", "2"], "bob1", library="personal"
        )

    def test_missing_foreign_personal_playlist_cannot_be_created_for_login(self):
        stem = OwnerAliases(self.lib).filename(("personal", "bob", "missing"))
        self.local_playlist(stem)
        with self.assertRaisesRegex(ui.UserError, "another owner"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_query_empty_foreign_missing_playlist_is_left_untouched(self):
        stem = OwnerAliases(self.lib).filename(("personal", "bob", "missing"))
        self.local_playlist(stem, [os.fsdecode(self.two.path)])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), ["title:one"]
        )
        self.client.save_playlist.assert_not_called()

    def test_known_encoded_missing_shared_playlist_preserves_creation_category(self):
        stem = OwnerAliases(self.lib).filename(("shared", "alice", "missing"))
        self.local_playlist(stem)
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno",)), []
        )
        self.client.save_playlist.assert_called_once_with(
            "missing", ["1"], None, library="shared"
        )

    def test_own_encoded_missing_playlist_does_not_update_foreign_homonym(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="bob1")
        ]
        stem = OwnerAliases(self.lib).filename(("shared", "alice", "favourites"))
        self.local_playlist(stem)
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], None, library="shared"
        )

    def test_library_filter_excludes_unknown_alias_in_other_category(self):
        self.local_playlist("P_unknown_favourites")
        self.client.playlists.return_value = []
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--library=shared")), []
        )
        self.client.save_playlist.assert_not_called()

    def test_library_filter_excludes_missing_owner_in_other_category(self):
        self.client.playlists.return_value.append(
            self.remote_playlist(owner="", library="shared")
        )
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--library=personal",)), []
        )
        self.assertEqual(os.listdir(self.playlists), ["P_alice_favourites.m3u"])

    def test_export_merge_preserves_owner_category_and_order(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="g1")
        ]
        stem = OwnerAliases(self.lib).filename(("shared", "bob", "favourites"))
        self.local_playlist(stem, [os.fsdecode(self.one.path), os.fsdecode(self.two.path)])
        self.client.playlist_songs.return_value = [
            {"id": "2"}, {"id": "3"}
        ]
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--conflict=merge")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["2", "3", "1"], "g1", library="shared"
        )

    def test_permission_errors_for_shared_existing_owner_propagate(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="g1")
        ]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.client.save_playlist.side_effect = SynologyError("permission denied")
        with self.assertRaisesRegex(ui.UserError, "permission denied"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1", "2"], "g1", library="shared"
        )

    def test_missing_personal_owner_uses_effective_username(self):
        del self.client.playlists.return_value[0]["owner"]
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--synouser=bob",)), [])
        self.assertEqual(os.listdir(self.playlists), ["P_bob_favourites.m3u"])

    def test_missing_shared_owner_roundtrips_without_guessing_connected_user(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="", library="shared", playlist_id="g1")
        ]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(os.listdir(self.playlists), ["G__favourites.m3u"])
        aliases = OwnerAliases(self.lib)
        self.assertEqual(aliases.identity("G__favourites"),
                         ("shared", None, "favourites"))
        with self.lib.transaction() as tx:
            self.assertEqual(tx.query("SELECT * FROM synologysync_owner_aliases"), [])
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1", "2"], "g1", library="shared"
        )

    def test_unprefixed_name_never_matches_unique_ownerless_shared_remote(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="", library="shared", playlist_id="g1")
        ]
        self.local_playlist("favourites")
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], None, library="personal"
        )

    def test_unprefixed_name_never_matches_unique_foreign_shared_remote(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="g1")
        ]
        self.local_playlist("favourites")
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], None, library="personal"
        )

    def test_unprefixed_name_never_matches_unique_current_shared_remote(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="alice", library="shared", playlist_id="g1")
        ]
        self.local_playlist("favourites")
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], None, library="personal"
        )

    def test_unprefixed_name_never_matches_unique_foreign_personal_remote(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="personal", playlist_id="bob1")
        ]
        self.local_playlist("favourites")
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], None, library="personal"
        )

    def test_unprefixed_name_targets_configured_shared_current_owner_only(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="bob1"),
            self.remote_playlist(owner="alice", library="personal", playlist_id="p1"),
            self.remote_playlist(owner="alice", library="shared", playlist_id="alice1"),
        ]
        self.local_playlist("favourites")
        self.plugin.config["playlist_library"] = "shared"
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], "alice1", library="shared"
        )

    def test_unprefixed_shared_default_creates_current_owner_not_foreign_homonym(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="bob1")
        ]
        self.local_playlist("favourites")
        self.plugin.config["playlist_library"] = "shared"
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--library=shared", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], None, library="shared"
        )

    def test_unprefixed_shared_default_does_not_match_ownerless_normal_target(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="", library="shared", playlist_id="unknown1")
        ]
        self.local_playlist("favourites")
        self.plugin.config["playlist_library"] = "shared"
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--library=shared", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1"], None, library="shared"
        )

    def test_duplicate_ownerless_shared_names_are_ambiguous_even_with_force(self):
        self.client.playlists.return_value = [
            self.remote_playlist(owner="", library="shared", playlist_id="g1"),
            self.remote_playlist(owner="", library="shared", playlist_id="g2"),
        ]
        self.local_playlist("G__favourites")
        for dest in ("beet", "syno"):
            with self.subTest(dest=dest), self.assertRaisesRegex(ui.UserError, "Ambiguous"):
                self.plugin.sync_playlists(
                    self.lib, self.options(arguments=(f"--dest={dest}", "--force")), []
                )
        self.client.save_playlist.assert_not_called()

    def test_ownerless_encoded_missing_target_cannot_be_created_for_login(self):
        self.local_playlist("G__missing")
        with self.assertRaisesRegex(ui.UserError, "unavailable owner"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_ownerless_encoded_target_does_not_match_newly_available_owner(self):
        self.local_playlist("G__favourites")
        self.client.playlists.return_value = [
            self.remote_playlist(owner="bob", library="shared", playlist_id="g1")
        ]
        with self.assertRaisesRegex(ui.UserError, "unavailable owner"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_known_owner_encoded_target_does_not_match_ownerless_remote(self):
        stem = OwnerAliases(self.lib).filename(("shared", "alice", "favourites"))
        self.local_playlist(stem)
        self.client.playlists.return_value = [
            self.remote_playlist(owner="", library="shared", playlist_id="g1")
        ]
        with self.assertRaisesRegex(ui.UserError, "owner is unavailable"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_ownerless_shared_name_escaping_remains_reversible(self):
        name = "G_under_scores/100%"
        self.client.playlists.return_value = [
            self.remote_playlist(owner="", library="shared", playlist_id="g1", name=name)
        ]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        stem = os.path.splitext(os.listdir(self.playlists)[0])[0]
        self.assertEqual(stem, "G__%47_under_scores%2F100%25")
        self.assertEqual(OwnerAliases(self.lib).identity(stem), ("shared", None, name))
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force", "-p", stem)), []
        )
        self.client.save_playlist.assert_called_once_with(
            name, ["1", "2"], "g1", library="shared"
        )

    def test_category_id_and_additional_owner_metadata(self):
        self.client.playlists.return_value = [
            {"id": "playlist_shared_normal/favourites", "name": "favourites",
             "additional": {"owner": "bob"}, "type": "normal"}
        ]
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.assertEqual(os.listdir(self.playlists), ["G_bob_favourites.m3u"])

    def test_plain_import_collision_is_not_silently_migrated_or_overwritten(self):
        path = self.local_playlist("favourites", [os.fsdecode(self.two.path)])
        self.plugin.sync_playlists(self.lib, self.options(arguments=("--force",)), [])
        self.assertEqual(self.plugin._read_playlist(path, self.playlists),
                         [os.fsdecode(self.two.path)])
        self.assertIn("P_alice_favourites.m3u", os.listdir(self.playlists))

    def test_plain_and_encoded_duplicate_local_identity_is_rejected(self):
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.local_playlist("favourites")
        with self.assertRaisesRegex(ui.UserError, "Ambiguous local playlist identity"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "--force")), []
            )
        self.client.save_playlist.assert_not_called()

    def test_escaped_reserved_prefix_local_playlist_creation(self):
        self.local_playlist("%47_fake_name")
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "-p", "G_fake_name")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "G_fake_name", ["1"], None, library="personal"
        )

    def test_export_ids_are_refreshed_from_api_not_stored(self):
        self.plugin.sync_playlists(self.lib, self.options(), [])
        self.client.playlists.return_value[0]["id"] = "replacement_id"
        self.plugin.sync_playlists(
            self.lib, self.options(arguments=("--dest=syno", "--force")), []
        )
        self.client.save_playlist.assert_called_once_with(
            "favourites", ["1", "2"], "replacement_id", library="personal"
        )

    def test_imported_flat_files_are_beets_playlist_query_compatible(self):
        for relative in ("playlist", "library", self.root):
            with self.subTest(relative=relative):
                config["playlist"]["relative_to"] = relative
                self.plugin.sync_playlists(
                    self.lib, self.options(arguments=("--force",)), ["title:one"]
                )
                query = PlaylistQuery("playlist", "P_alice_favourites", True)
                self.assertEqual([item.id for item in self.lib.items(query)], [self.one.id])

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

    def test_rating_sync_log_includes_old_and_new_values(self):
        self.prepare_ratings()
        with patch.object(self.plugin._log, "info") as info:
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        info.assert_any_call(
            "Synced rating to {}: {} (rating {} -> {})", "beet", self.one.path, None, 8
        )

    def test_matching_path_comparison_is_logged_in_green(self):
        self.prepare_ratings()
        with patch("beetsplug.synologysync.ui.colorize",
                   side_effect=lambda color, text: f"<{color}>{text}") as colorize, \
                patch.object(self.plugin._log, "debug") as debug:
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        colorize.assert_any_call(
            "text_success", "Comparing AudioStation path '{}' with mapped path '{}': {}"
        )
        self.assertTrue(any(
            c.args[0].startswith("<text_success>Comparing") and c.args[3] == "match"
            for c in debug.call_args_list
        ))

    def test_rating_import_uses_fallback_username(self):
        parse = self.prepare_ratings()
        self.plugin.config["synouser"] = ""
        config["userrating"].set({"default_user": "fallback"})
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.one.load()
        self.assertEqual(parse(self.one.get("userrating")), {"beets-fallback": 8})
        self.assertEqual(self.client_patch.target.AudioStation.call_args.args[1], "fallback")

    def test_rating_import_filters_source_not_destination(self):
        parse = self.prepare_ratings()
        self.one["userrating"] = {"other": 3, "beets-alice": 2}
        self.one.store()
        with patch("beetsplug.synologysync.ui.input_options", return_value="u") as prompt:
            self.plugin.sync_ratings(
                self.lib, self.options(0, ("--rating=8", "--dest=beet")), []
            )
        prompt.assert_called_once()
        self.client.songs.assert_called_once_with(rating=4)
        self.one.load()
        self.two.load()
        self.assertEqual(parse(self.one.get("userrating")), {"other": 3, "beets-alice": 8})
        self.assertEqual(parse(self.two.get("userrating")), {})

    def test_rating_import_intersects_beets_query(self):
        parse = self.prepare_ratings()
        self.plugin.sync_ratings(
            self.lib, self.options(0, ("--rating=8",)), ["title:two"]
        )
        self.client.songs.assert_called_once_with(title="two", rating=4)
        self.one.load()
        self.two.load()
        self.assertEqual(parse(self.one.get("userrating")), {})
        self.assertEqual(parse(self.two.get("userrating")), {})

    def test_rating_import_zero_and_filtered_remote_results(self):
        parse = self.prepare_ratings()
        self.client.songs.return_value = [self.client.songs.return_value[1]]
        with patch.object(self.plugin._log, "warning") as warning:
            self.plugin.sync_ratings(self.lib, self.options(0, ("--rating=0",)), [])
        warning.assert_not_called()
        self.client.songs.assert_called_once_with(rating=0)
        self.two.load()
        self.assertEqual(parse(self.two.get("userrating")), {"beets-alice": 0})

    def test_invalid_rating_rejected_before_connection(self):
        for rating in ("-1", "11", "3"):
            with self.subTest(rating=rating), self.assertRaises(ui.UserError):
                self.plugin.sync_ratings(
                    self.lib, self.options(0, ("--rating=" + rating,)), []
                )
        self.password.assert_not_called()

    def test_rating_option_requires_integer(self):
        with self.assertRaises(SystemExit):
            self.options(0, ("--rating=4.5",))

    def test_rating_export_filters_selected_user_and_query(self):
        self.prepare_ratings()
        self.one["userrating"] = {"beets-bob": 6, "beets-alice": 2}
        self.two["userrating"] = {"beets-bob": 8, "beets-alice": 6}
        self.one.store()
        self.two.store()
        opts = self.options(
            0, ("--rating=6", "--dest=syno", "--beetuser=bob", "--force")
        )
        self.plugin.sync_ratings(self.lib, opts, ["title:two"])
        self.password.assert_not_called()
        self.plugin.sync_ratings(self.lib, opts, [])
        self.client.songs.assert_called_once_with()
        self.client.set_rating.assert_called_once_with("1", 3)

    def test_rating_export_odd_zero_and_ten(self):
        self.prepare_ratings()
        for rating, stars in ((7, 4), (0, 0), (10, 5)):
            with self.subTest(rating=rating):
                self.client.set_rating.reset_mock()
                self.one["userrating"] = {"beets-alice": rating}
                self.one.store()
                self.client.songs.return_value[0]["additional"]["song_rating"]["rating"] = 2
                self.plugin.sync_ratings(
                    self.lib,
                    self.options(0, (f"--rating={rating}", "--dest=syno", "--force")),
                    [],
                )
                self.client.set_rating.assert_called_once_with("1", stars)

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

    def test_sync_ratings_progress_and_connection_logs(self):
        self.prepare_ratings()
        with patch.object(self.plugin._log, "info") as log_info:
            self.plugin.sync_ratings(self.lib, self.options(0), [])
        calls = [c.args[0] for c in log_info.call_args_list]
        self.assertIn("Connecting to AudioStation...", calls)
        self.assertIn("Connected to AudioStation", calls)
        self.assertIn("Fetching songs from AudioStation...", calls)
        self.assertIn("Indexed {} songs from AudioStation", calls)
        self.assertIn("Processing song ({}/{}): {}", calls)

    def test_song_index_pagination_progress_log(self):
        many_songs = [
            {"id": str(i), "path": f"/music/{i}.mp3", "additional": {"song_rating": {"rating": 0}}}
            for i in range(1005)
        ]
        self.client.songs.return_value = many_songs
        with patch.object(self.plugin._log, "info") as log_info:
            self.plugin._song_index(self.client)
        calls = [c.args[0] for c in log_info.call_args_list]
        self.assertIn("Fetching songs from AudioStation...", calls)
        self.assertIn("Fetched {} songs from AudioStation...", calls)
        self.assertIn("Indexed {} songs from AudioStation", calls)

    def test_missing_mapped_song_warning_unmatched_local_path(self):
        self.prepare_ratings()
        unmapped_item = Item(
            path=os.fsencode("/different/directory/unmapped.mp3"), title="unmapped"
        )
        self.lib.add(unmapped_item)
        with patch.object(self.plugin._log, "warning") as warning:
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:unmapped"])
        warning.assert_called_once()
        self.assertEqual(
            warning.call_args.args[0],
            "No mapped AudioStation song for {}: path does not match any path_mapping",
        )
        self.assertEqual(warning.call_args.args[1], unmapped_item.path)

    def test_missing_mapped_song_warning_unmatched_remote_path(self):
        self.prepare_ratings()
        item_three = Item(
            path=os.fsencode(os.path.join(self.music, "three.mp3")), title="three"
        )
        self.lib.add(item_three)
        with patch.object(self.plugin._log, "warning") as warning:
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:three"])
        warning.assert_called_once()
        self.assertEqual(
            warning.call_args.args[0],
            "No mapped AudioStation song for {}: mapped path '{}' not found in AudioStation",
        )
        self.assertEqual(warning.call_args.args[1], item_three.path)
        self.assertEqual(warning.call_args.args[2], "/music/three.mp3")

    def test_per_song_search_logs_path_comparisons_and_similar_paths(self):
        self.prepare_ratings()
        other = {"id": "9", "path": "/volume1/music/one.mp3",
                 "additional": {"song_rating": {"rating": 4}}}
        self.client.songs.reset_mock()
        self.client.songs.side_effect = None
        self.client.songs.return_value = [other]
        with patch.object(self.plugin._log, "debug") as debug, \
                patch.object(self.plugin._log, "info") as info:
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        debug_calls = [c.args for c in debug.call_args_list]
        self.assertIn((
            "Comparing AudioStation path '{}' with mapped path '{}': {}",
            "/volume1/music/one.mp3", "/music/one.mp3", "no match",
        ), debug_calls)
        self.assertTrue(any(
            c[0].startswith("Mapped local path '{}' to AudioStation path '{}'")
            and c[2] == "/music/one.mp3"
            for c in debug_calls
        ))
        info_calls = [c.args for c in info.call_args_list]
        self.assertTrue(any(
            c[0].startswith("AudioStation songs with the same file name as '{}'")
            and c[1:3] == ("/music/one.mp3", ["/volume1/music/one.mp3"])
            for c in info_calls
        ))

    def test_playlist_export_missing_mapped_song_distinguishes_reasons(self):
        # Entry outside path_mapping
        item_unmapped = Item(path=b"/outside/unmapped.mp3", title="unmapped")
        self.lib.add(item_unmapped)
        self.local_playlist("unmapped_playlist", ["/outside/unmapped.mp3"])
        with self.assertRaisesRegex(ui.UserError, "path does not match any path_mapping"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "-p", "unmapped_playlist")), []
            )

        # Entry within path_mapping but not indexed in AudioStation
        missing_remote = os.path.join(self.music, "nonexistent.mp3")
        item_missing = Item(path=os.fsencode(missing_remote), title="nonexistent")
        self.lib.add(item_missing)
        self.local_playlist("missing_remote_playlist", [missing_remote])
        with self.assertRaisesRegex(ui.UserError, "not found in AudioStation"):
            self.plugin.sync_playlists(
                self.lib, self.options(arguments=("--dest=syno", "-p", "missing_remote_playlist")), []
            )

    def test_single_api_call_threshold_per_song_vs_global(self):
        self.prepare_ratings()
        # Under threshold (default 100): 1 item matching query -> per-song search
        self.client.songs.reset_mock()
        self.client.songs.return_value = [
            {"id": "1", "path": "/music/one.mp3", "additional": {"song_rating": {"rating": 4}}}
        ]
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.client.songs.assert_called_once_with(title="one", rating=None)

        # Configured threshold = 1: 1 item matching query >= threshold -> global call
        self.plugin.config["SingleAPICallThreshold"] = 1
        self.client.songs.reset_mock()
        self.client.songs.return_value = [
            {"id": "1", "path": "/music/one.mp3", "additional": {"song_rating": {"rating": 4}}},
            {"id": "2", "path": "/music/two.mp3", "additional": {"song_rating": {"rating": 0}}},
        ]
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.client.songs.assert_called_once_with()

        # No query (empty args): always global call regardless of item count
        self.plugin.config["SingleAPICallThreshold"] = 100
        self.client.songs.reset_mock()
        self.plugin.sync_ratings(self.lib, self.options(0), [])
        self.client.songs.assert_called_once_with()

    def test_per_song_search_falls_back_to_album_then_artist(self):
        self.prepare_ratings()
        self.one.album = "Album"
        self.one.artist = "Artist"
        self.one.store()
        match = {"id": "1", "path": "/music/one.mp3", "additional": {"song_rating": {"rating": 4}}}
        other = {"id": "9", "path": "/music/other.mp3", "additional": {"song_rating": {"rating": 4}}}

        self.client.songs.reset_mock()
        self.client.songs.side_effect = [[other], [match]]
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.assertEqual(self.client.songs.call_args_list, [
            call(title="one", rating=None),
            call(album="Album", rating=None),
        ])

        self.client.songs.reset_mock()
        self.client.songs.side_effect = [[], [other], [match]]
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.assertEqual(self.client.songs.call_args_list, [
            call(title="one", rating=None),
            call(album="Album", rating=None),
            call(artist="Artist", rating=None),
        ])

        self.client.songs.reset_mock()
        self.client.songs.side_effect = [[], [], []]
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.assertEqual(self.client.songs.call_count, 3)

    def test_per_song_search_skips_empty_fields(self):
        self.prepare_ratings()
        self.one.artist = "Artist"
        self.one.store()
        self.client.songs.reset_mock()
        self.client.songs.side_effect = [[], []]
        self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])
        self.assertEqual(self.client.songs.call_args_list, [
            call(title="one", rating=None),
            call(artist="Artist", rating=None),
        ])

    def test_single_api_call_threshold_invalid(self):
        self.prepare_ratings()
        self.plugin.config["SingleAPICallThreshold"] = "invalid"
        with self.assertRaisesRegex(ui.UserError, "must be an integer"):
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])

    def test_sync_ratings_per_song_ambiguous_result_raises(self):
        self.prepare_ratings()
        self.client.songs.return_value = [
            {"id": "1", "path": "/music/one.mp3", "additional": {"song_rating": {"rating": 4}}},
            {"id": "99", "path": "/music/one.mp3", "additional": {"song_rating": {"rating": 4}}},
        ]
        with self.assertRaisesRegex(ui.UserError, "Ambiguous AudioStation song path"):
            self.plugin.sync_ratings(self.lib, self.options(0), ["title:one"])


if __name__ == "__main__":
    unittest.main()
