"""The packaged Replay UI build, loaded once into an in-memory allowlist.

The Replay server never maps a request path to the file system. At startup this module
reads the known build files from the fixed package location ``agentbodycam/ui/static``
(``index.html`` plus regular files with an allowed extension, at most one directory deep,
no links, bounded sizes) into a dictionary keyed by their exact URL path, for example
``/assets/index-3f2a9c1b.js``. A request is served only if its raw, undecoded path is a key
of that dictionary, so ``..``, percent-encoding, backslashes, project files and anything
under ``.agentbodycam/`` can never resolve to anything.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType

INDEX_NAME = "index.html"
ASSET_DIRECTORY = "assets"
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_ASSET_COUNT = 256

# Extension -> Content-Type. HTML is deliberately absent: the only document is index.html,
# which is served for UI routes and never under its own file name.
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+$")


class UiUnavailableError(RuntimeError):
    """The packaged UI build is missing or unusable."""


@dataclass(frozen=True, slots=True)
class UiAsset:
    body: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class UiBundle:
    """``index`` is the UI document; ``assets`` maps exact URL paths to static files."""

    index: UiAsset
    assets: Mapping[str, UiAsset]

    def asset(self, path: str) -> UiAsset | None:
        return self.assets.get(path)


def packaged_ui_root() -> Traversable:
    return resources.files("agentbodycam.ui").joinpath("static")


def _is_link(entry: Traversable) -> bool:
    """Symlinks, and on Windows any reparse point (junctions included), are never read."""
    if not isinstance(entry, Path):
        return False  # e.g. a zip member: no links possible
    try:
        st = entry.lstat()
    except OSError:
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _files(root: Traversable) -> Iterator[tuple[str, Traversable]]:
    """``(url_path, entry)`` for each candidate file: top level and ``assets/`` only."""
    for entry in root.iterdir():
        if _is_link(entry):
            continue
        if entry.is_file():
            yield f"/{entry.name}", entry
        elif entry.is_dir() and entry.name == ASSET_DIRECTORY:
            for child in entry.iterdir():
                if not _is_link(child) and child.is_file():
                    yield f"/{ASSET_DIRECTORY}/{child.name}", child


def load_ui_bundle(root: Traversable | None = None) -> UiBundle:
    """Read the UI build at ``root`` (default: the packaged one) into memory."""
    root = packaged_ui_root() if root is None else root
    if _is_link(root) or not root.is_dir():
        raise UiUnavailableError("the Replay UI build is missing from this installation")
    index: UiAsset | None = None
    assets: dict[str, UiAsset] = {}
    total = 0
    for url_path, entry in sorted(_files(root), key=lambda item: item[0]):
        name = entry.name
        suffix = name[name.rfind(".") :].lower() if "." in name else ""
        is_index = url_path == f"/{INDEX_NAME}"
        if not is_index and (_NAME.fullmatch(name) is None or suffix not in CONTENT_TYPES):
            continue
        body = entry.read_bytes()
        total += len(body)
        if len(body) > MAX_ASSET_BYTES or total > MAX_BUNDLE_BYTES:
            raise UiUnavailableError("the Replay UI build is unexpectedly large")
        if is_index:
            index = UiAsset(body, "text/html; charset=utf-8")
        else:
            assets[url_path] = UiAsset(body, CONTENT_TYPES[suffix])
            if len(assets) > MAX_ASSET_COUNT:
                raise UiUnavailableError("the Replay UI build has unexpectedly many files")
    if index is None:
        raise UiUnavailableError("the Replay UI build has no index.html")
    return UiBundle(index=index, assets=MappingProxyType(assets))
