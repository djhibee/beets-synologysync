# beets-synologysync
Beets plugin that synchronizes Synology AudioStation song ratings and playlists.

Licensed under the GNU General Public License v3.0; see [LICENSE](LICENSE).

## Installation

Requires Python 3.10+ and beets 2.0+. Install into the same Python environment
as beets:

```sh
python -m pip install .
```

Enable `synologysync`, and enable
[userrating](https://github.com/djhibee/beets-userrating) for ratings and
[playlist](https://beets.readthedocs.io/en/stable/plugins/playlist.html) for
playlists. The userrating installation must support per-user ratings
(`beetsplug.userrating.ratings`); older single-rating releases are not supported.

## Configuration

```yaml
plugins: [userrating, playlist, synologysync]

synologysync:
  host: https://nas.example.org:5001
  # public_url: https://nas.example.org:5001
  synouser: alice
  # beetuser: alice  # defaults to synouser
  path_mapping:
    - local: /home/alice/Music
      syno: /music
  force: "no"        # no | rating | playlist | yes
  timeout: 30
  # ca_file: /home/alice/nas-ca.pem

playlist:
  playlist_dir: /home/alice/Playlists
  relative_to: library  # library | playlist | an explicit directory
  forward_slash: false
```

The AudioStation username comes from `--synouser`, then
`synologysync.synouser`. When neither is set, it falls back to
`userrating.autorate.user`, `userrating.default_user`, the first entry in
`userrating.beets_users`, then `"defaultUser"`, in that order. Empty values
are treated as unset. This effective username also supplies the default
rating user unless `--beetuser` or `synologysync.beetuser` overrides it.

`syno` is the path prefix **as reported by AudioStation's song API**, not
necessarily a DSM filesystem path such as `/volume1/music`. Multiple mappings
are supported; the longest matching local prefix wins. Matches respect directory
boundaries. Songs are matched by mapped path, never just by filename or title.

Connections require HTTPS with certificate and hostname verification, even on a
local network. For a private CA, set `ca_file` to its PEM certificate bundle.
There is no insecure/disable-verification option. Redirects are refused to avoid
forwarding credentials. Passwords are prompted for every invocation, sent only
in HTTPS POST bodies, and never stored in configuration or the library. Sessions
are logged out on completion, including when synchronization fails. Grant the
Synology account AudioStation access and write permissions for exports.

### Connect to a local IP using a public certificate

To connect directly to your NAS's local IP without changing DNS or your hosts
file, set `host` to the local HTTPS address and `public_url` to the HTTPS origin
whose hostname is covered by the certificate:

```yaml
synologysync:
  host: https://192.168.1.10:5001
  public_url: https://nas.example.org:5001
```

The urllib3 client connects to the address and port in `host` (or `--url`),
but uses the hostname in `public_url` for TLS SNI and certificate hostname
verification. The HTTP `Host` header uses the authority from `public_url`,
including its port if specified. The public hostname is not resolved or
contacted. Connection ports may differ; `public_url` never changes the actual
connection destination or API path.
This mode also bypasses environment proxies to keep the connection direct.
Without `public_url`, HTTPS proxy settings and `no_proxy` exclusions continue
to apply.

`public_url` is optional and must be an HTTPS origin, without credentials,
query, fragment, or a path other than `/`. When omitted, the connection URL
determines both TLS identity and `Host`, as before. Certificate verification
remains mandatory: the certificate must be valid and trusted, and cover the
public hostname. Use `ca_file` for a private CA; this setting does not accept
expired, untrusted, or mismatched certificates. Redirects remain forbidden,
and authenticated requests are not automatically retried.

## Ratings

```sh
beet syncRatings artist:Example
beet syncRatings --dest=syno --force album:Example
beet syncRatings --url=https://nas.example.org:5001 --synouser=alice --beetuser=bob
```

The default destination is `beet`; `--dest=syno` exports to AudioStation.
Normal beets item queries are accepted; no query selects the entire library.
The ratings mapping uses the key `beets-<user>`, where `<user>` comes from
`--beetuser`, `beetuser` configuration, or the effective Synology username, in
that order. Other users' ratings are left unchanged. This key is the userrating
database key; userrating independently controls how it names file tags. Ratings
are stored in the beets database; use beets' normal tag-writing workflow if
you also want file tags updated.

AudioStation's 0–5 ratings are mapped to userrating's 0–10 scale. Export rounds
odd ratings to the next whole star (7 becomes 4). Explicit zero is synchronized;
an absent beets user rating is skipped during export. Missing mapped songs are
reported and skipped. Different existing destination ratings prompt for
**update/keep**; `--force` or `force: rating/yes` updates without prompting.
Equal ratings require no change.

## Playlists

```sh
beet syncPlaylist
beet syncPlaylist -p favourites artist:Example
beet syncPlaylist --dest=syno --conflict=merge
beet syncPlaylist --dest=syno --force -p favourites
```

This command works in both directions.
It reads the playlist plugin's `playlist_dir`, `relative_to`, and
`forward_slash` settings. Imports create UTF-8 M3U playlists; exports read
`.m3u` and `.m3u8` files in `playlist_dir`. `-p/--playlist` selects an exact
playlist name, without an extension.

Queries filter playlist membership to matching beets items, preserving order.
With a query, playlists with no matching songs are left untouched. With no query,
all library items are eligible, and empty playlists can be synchronized.
Imports skip songs not found in the selected beets items. Exports fail before
changing a playlist if any selected song cannot be mapped to AudioStation.

For an existing destination playlist, choose **replace** (default), **skip**,
or **merge**. Merge keeps existing songs first, appends incoming songs, and
removes duplicates. Use `--conflict=replace|skip|merge` to force that policy;
`--force` or `force: playlist/yes` selects replace when no policy is supplied.
Replace with a query replaces the destination membership with the filtered
selection; use merge to retain other songs. Duplicate playlist names and unsafe
filenames are rejected rather than choosing an arbitrary target. Local writes
are atomic.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use a temporary beets database and mocked AudioStation transport. A real
NAS is not needed for the tests; verify API compatibility and account permissions
on your AudioStation installation before performing a bulk export.
Local HTTPS tests additionally generate temporary CA and server certificates
with OpenSSL and verify certificate checks, SNI, `Host`, and redirect rejection
over loopback. Those tests are skipped if the `openssl` executable is unavailable;
generated private keys are kept only in temporary directories and removed after
testing.
