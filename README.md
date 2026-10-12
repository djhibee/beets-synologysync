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
  library: all      # all | personal | shared: playlist selection
  playlist_library: personal  # personal | shared: new unprefixed playlists
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
beet syncSynoRatings artist:Example
beet syncSynoRatings --dest=syno --force album:Example
beet syncSynoRatings --url=https://nas.example.org:5001 --synouser=alice --beetuser=bob
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
beet syncSynoPlaylist
beet syncSynoPlaylist -p favourites artist:Example
beet syncSynoPlaylist --dest=syno --conflict=merge
beet syncSynoPlaylist --dest=syno --force -p favourites
beet syncSynoPlaylist --library=shared
beet syncSynoPlaylist --dest=syno --force -p G_alice_favourites
```

This command works in both directions.
It reads the playlist plugin's `playlist_dir`, `relative_to`, and
`forward_slash` settings. Imports create UTF-8 M3U playlists; exports read
`.m3u` and `.m3u8` files directly in `playlist_dir` (no subdirectories).
`--library=all|personal|shared` overrides `synologysync.library` (default `all`).
Only accessible, non-smart playlists are imported; existing smart export targets
are skipped, never overwritten or recreated as normal playlists.

### Playlist identity and filenames

Imports use flat filenames:

* `G_<owner alias>_<original name>.m3u` for shared playlists.
* `P_<owner alias>_<original name>.m3u` for personal playlists.

For example, Alice's personal and shared `favourites` playlists become
`P_alice_favourites.m3u` and `G_alice_favourites.m3u`. Playlists of different
owners also remain separate. Owner aliases contain at most eight ASCII letters,
digits, or hyphens. Unsafe owner characters (including underscores) are replaced
in the alias; truncation/sanitization collisions get numeric suffixes. The exact
owner is retained in the beets SQLite table `synologysync_owner_aliases`, so aliases
remain stable across runs and database reopening. Keep this database with the
playlist files: unknown aliases fail rather than guessing an owner.
Before allocating aliases for imports, every existing encoded filename is
validated against the saved map, even across category selections. A lost or
incomplete alias table therefore cannot silently reassign an old filename to a
new owner with the same shortened alias; restore the map before importing.
When a shared playlist's owner is unavailable in the API, its explicit filename
is `G__<original name>.m3u`: the empty alias means **unknown owner**, not the login
user, and does not create an alias-table entry.

Original names retain underscores; filenames are parsed with only the first two
underscores as delimiters. UTF-8 percent escaping preserves unsafe characters,
literal percent signs, dots, trailing spaces, and reserved `G_`/`P_` name
prefixes. Thus even a name containing `/` can round-trip without creating a
subdirectory. Names exceeding the 255-byte encoded filename limit fail instead
of being truncated. Do not rename the category/owner prefix unless intentionally
changing the remote identity.

`-p/--playlist` selects an exact filename stem (without extension) or the original
AudioStation name. An exact existing stem takes precedence. Importing by original
name can select homonyms across categories/owners. On export, an original name
matching multiple local identities fails; select the generated stem or narrow
`--library`. Ordinary beets queries, e.g. `beet ls playlist:P_alice_favourites`,
work with the imported files, including `relative_to: playlist`.

Exports resolve **category + full owner + original name** against the current API
response, using the remote ID only for that operation. Remote IDs are never stored.
Duplicate remote identities and multiple local files
targeting one identity fail before any playlist is written, even with `--force`.
There is no multiple-NAS namespace: use separate beets databases/playlist
directories for different servers.

An unprefixed local playlist always targets the triple consisting of
`synologysync.playlist_library` (default `personal`), the connected username, and
its original name. It updates only that exact remote identity, or creates it
when absent. Same-name playlists in another category or owned by another user
are left untouched; name uniqueness alone never retargets an unprefixed file.
Set `playlist_library: shared` to target/create your own shared playlists.
Ownerless shared targets require their explicit imported `G__` filename, not
a plain name. `--library` selects targets, not the
creation category; a creation category outside that selection is an error.
To create a new playlist whose literal name starts with `G_` or `P_`, escape its
first letter in the local filename, e.g. `%47_favourites.m3u` for `G_favourites`.
Imports leave older plain filenames untouched and create encoded files instead;
remove or explicitly select the older files before exporting if both identify
the same destination.

Missing personal owners fall back to the effective login username. Shared owner
metadata is used when supplied by the API (top-level `owner` or
`additional.owner`). An ownerless shared filename matches only a remote shared
playlist with the same original name and still-unavailable owner; multiple such
entries are ambiguous and fail even with force. If that remote playlist disappears
or later gains owner metadata, export fails instead of guessing or creating for
the login user: reimport to obtain the updated identity. Likewise, an encoded
known-owner filename never updates an ownerless normal remote target. If an
encoded playlist's owner is no longer present remotely, a new playlist is never
silently created for a different logged-in user. Existing shared owners are honored via
the resolved remote ID; permission errors propagate without category fallback.
For the same reason, ambiguous ownership is an error, not a force-overwritable
conflict.

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
selection; use merge to retain other songs. Homonyms are supported through their
encoded identities; unresolved ambiguity is always rejected rather than choosing
an arbitrary target. Local writes are atomic.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use a temporary beets database and mocked AudioStation transport. A real
NAS is not needed for the tests; verify API compatibility and account permissions
on your AudioStation installation before performing a bulk export.
Native `playlist:` CLI queries were also verified with beets 2.0. Beets 2.14.1
has a built-in playlist query limitation for libraries storing relative database
paths: its playlist query compares absolute paths to those relative entries.
The M3U files remain readable and synchronization still works; that upstream
query limitation is not worked around by this plugin.
Local HTTPS tests additionally generate temporary CA and server certificates
with OpenSSL and verify certificate checks, SNI, `Host`, and redirect rejection
over loopback. Those tests are skipped if the `openssl` executable is unavailable;
generated private keys are kept only in temporary directories and removed after
testing.
