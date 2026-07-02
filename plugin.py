from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import sublime
import sublime_plugin

from LSP.plugin import (
    AbstractPlugin,
    Notification,
    Request,
    register_plugin,
    unregister_plugin,
)
from LSP.plugin import LspTextCommand

# ─── Constants ─────────────────────────────────────────────────────────────────

PACKAGE_NAME = "LSP-tads3tools"
SERVER_NAME = "tads3"
STATUS_KEY = "tads3tools"

# ─── Module-level state ────────────────────────────────────────────────────────

# Parse state — updated from the server notification handlers.
# Read on the main thread for status bar updates.
_state: Dict[str, Any] = {
    "parsing": False,
    "file": "",
    "tracker": 0,
    "total": 0,
    "pool_size": 0,
    "using_adv3_lite": False,
    "makefile_kvs": [],
    "preprocessed": [],
    # True while a single targeted file is parsing. The server never sends
    # symbolparsing/allfiles/success for a single-file parse (see
    # parse-workers-manager.ts), so symbolparsing/success is the only
    # completion signal that ever arrives for one — this flag tells
    # _on_file_success to treat it as terminal instead of mere progress.
    "single_file": False,
}

# Per-session cache: project_root → absolute makefile path.
# Cleared by Tads3SelectMakefileCommand.
_selected_makefiles: Dict[str, str] = {}

# project_root → True once auto-parsed this session, so merely switching between
# tabs of an already-parsed project doesn't re-trigger a parse. Reset whenever a
# fresh session initializes, since a new server process has an empty symbol table.
_auto_parsed_roots: set = set()

# project_root → set of absolute file paths saved since the last targeted re-parse
# fired. Accumulated (not replaced) so a burst of saves (e.g. Save All) re-parses
# every changed file in one request instead of only whichever file saved last.
_pending_save_files: Dict[str, set] = {}

# project_root → generation counter, used to debounce save-triggered re-parses so
# a burst of saves collapses into a single trailing request.
_save_reparse_generation: Dict[str, int] = {}
_SAVE_REPARSE_DEBOUNCE_MS = 500

# Weak reference to the most recently ready plugin instance.
# Commands use this to reach the session without coupling to view state.
_plugin_instance: Optional[weakref.ref] = None

# ─── Platform helpers ──────────────────────────────────────────────────────────


def _platform_info() -> Optional[Tuple[str, str]]:
    """Return (platform_suffix, binary_extension) or None for unsupported platforms."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        return ("macos-arm64" if machine == "arm64" else "macos-x64", "")
    if system == "Linux":
        return ("linux-arm64" if machine in ("aarch64", "arm64") else "linux-x64", "")
    if system == "Windows":
        return ("win-arm64" if "arm" in machine else "win-x64", ".exe")
    return None


def _default_bin_dir() -> str:
    # Store alongside Sublime's Local/ packages so it survives package updates.
    return os.path.join(
        os.path.dirname(sublime.packages_path()), "Local", "tads3tools", "bin"
    )


def _server_binary() -> Optional[str]:
    """Return the path of an executable server binary, or None."""
    info = _platform_info()
    if not info:
        return None
    suffix, ext = info
    path = os.path.join(
        _default_bin_dir(), "vscode-tads3tools-server-" + suffix + ext
    )
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _storage_path() -> str:
    return os.path.join(sublime.cache_path(), "tads3tools")


# ─── Project helpers ───────────────────────────────────────────────────────────


def _find_makefiles(from_path: str) -> List[str]:
    """Walk up from from_path to find the nearest directory with *.t3m files."""
    start = from_path if os.path.isdir(from_path) else os.path.dirname(from_path)
    search = start
    while True:
        if any(Path(search).glob("*.t3m")):
            paths = sorted(str(p) for p in Path(search).rglob("*.t3m"))
            return paths
        parent = os.path.dirname(search)
        if parent == search:
            break
        search = parent
    return []


def _resolve_makefile(from_path: str) -> Optional[str]:
    """Return the cached makefile for this path's project, or None if ambiguous/missing."""
    paths = _find_makefiles(from_path)
    if not paths:
        return None
    root = os.path.dirname(paths[0])
    cached = _selected_makefiles.get(root)
    if cached:
        return cached
    if len(paths) == 1:
        _selected_makefiles[root] = paths[0]
        return paths[0]
    return None  # Multiple candidates — caller must prompt.


def _auto_parse_if_needed(buf_path: str) -> None:
    """Auto-select + parse a project's makefile the first time it's encountered
    this session. No-ops if ambiguous, already parsed, or no session is ready yet."""
    makefile = _resolve_makefile(buf_path)
    if not makefile:
        return
    root = os.path.dirname(makefile)
    if root in _auto_parsed_roots:
        return
    plugin = _plugin()
    if not plugin:
        return
    # Only mark the root as handled if the request was actually sent — if a
    # parse was already in flight, leave it unmarked so the next activation
    # (e.g. switching back to this tab) retries instead of silently giving up.
    if plugin.send_parse(makefile):
        _auto_parsed_roots.add(root)


def _find_output_file(makefile_path: str) -> Optional[str]:
    """Parse the -o <file> line in a .t3m makefile and return an absolute path."""
    makefile_dir = os.path.dirname(makefile_path)
    try:
        with open(makefile_path) as f:
            for line in f:
                m = re.match(r"^\s*-o\s+(\S+)", line)
                if m:
                    name = m.group(1)
                    return name if os.path.isabs(name) else os.path.join(makefile_dir, name)
    except OSError:
        pass
    return None


# ─── Status bar ────────────────────────────────────────────────────────────────


def _refresh_status() -> None:
    """Update the tads3tools status key in every open TADS3 view."""
    if _state["parsing"]:
        total = _state["total"]
        msg = (
            "[tads3 {}/{}]".format(_state["tracker"], total)
            if total > 0
            else "[tads3 …]"
        )
    else:
        msg = ""
    for window in sublime.windows():
        for view in window.views():
            syntax = view.syntax()
            if syntax and "tads3" in syntax.scope:
                if msg:
                    view.set_status(STATUS_KEY, msg)
                else:
                    view.erase_status(STATUS_KEY)


# ─── AbstractPlugin ────────────────────────────────────────────────────────────


class LspTads3toolsPlugin(AbstractPlugin):

    @classmethod
    def name(cls) -> str:
        return SERVER_NAME

    @classmethod
    def configuration(cls) -> Tuple[sublime.Settings, str]:
        basename = "{}.sublime-settings".format(PACKAGE_NAME)
        filepath = "Packages/{}/{}".format(PACKAGE_NAME, basename)
        return sublime.load_settings(basename), filepath

    @classmethod
    def additional_variables(cls) -> Optional[Dict[str, str]]:
        binary = _server_binary() or ""
        return {"server_path": binary}

    def on_server_response_async(self, method: str, response: Any) -> None:
        # The server sometimes returns bare filesystem paths (no "file://" scheme)
        # as Location/LocationLink uris — e.g. for #define macro definitions. Sublime's
        # LSP client rejects those with "URI scheme is unsupported", which breaks the
        # goto-definition/references quick panel. Normalize in place before it propagates.
        self._fix_bare_path_uris(getattr(response, "result", None))

        # LSP 2.13's Session only calls on_initialized_async for LspPlugin, not the
        # deprecated AbstractPlugin we use here. The initialize response is the
        # equivalent "session ready" signal for AbstractPlugin subclasses.
        if method != "initialize":
            return
        global _plugin_instance
        _plugin_instance = weakref.ref(self)
        # A freshly (re)started server has an empty symbol table, so forget which
        # projects were already auto-parsed by a previous session.
        _auto_parsed_roots.clear()

        session = self.weaksession()
        if not session:
            return

        # Dispatch table used by on_server_notification_async for custom
        # server-to-client notifications (this LSP version routes every
        # notification through that single hook — there is no per-method
        # registration API on Session for AbstractPlugin subclasses).
        self._notification_handlers: Dict[str, Callable] = {
            "symbolparsing/processing":       self._on_processing,
            "symbolparsing/success":          self._on_file_success,
            "symbolparsing/failed":           self._on_file_failed,
            "symbolparsing/allfiles/success": self._on_allfiles_success,
            "symbolparsing/allfiles/failed":  self._on_allfiles_failed,
            "response/preprocessed/list":     self._on_preprocessed_list,
            "response/makefile/keyvaluemap":  self._on_makefile_kvmap,
            # VS Code-only — silence "no handler" warnings.
            "response/mapsymbols":            lambda _p: None,
            "response/npcsymbols":            lambda _p: None,
            "response/foundsymbol":           lambda _p: None,
            "response/connectrooms":          lambda _p: None,
            "response/extractQuotes":         lambda _p: None,
            "response/preprocessed/file":     lambda _p: None,
            "response/analyzeText/findNouns": lambda _p: None,
        }

        sublime.set_timeout(self._trigger_initial_parse, 0)

    def on_server_notification_async(self, notification: Notification) -> None:
        handler = getattr(self, "_notification_handlers", {}).get(notification.method)
        if handler:
            handler(notification.params)

    @staticmethod
    def _fix_bare_path_uris(result: Any) -> None:
        if result is None:
            return
        items = result if isinstance(result, list) else [result]
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("uri", "targetUri"):
                value = item.get(key)
                if isinstance(value, str) and "://" not in value:
                    item[key] = "file://" + urllib.request.pathname2url(
                        os.path.abspath(value)
                    )

    def _trigger_initial_parse(self) -> None:
        window = sublime.active_window()
        view = window.active_view() if window else None
        buf_path = view.file_name() if view else None
        if buf_path:
            _auto_parse_if_needed(buf_path)

    def send_parse(
        self, makefile: str, file_paths: Optional[List[str]] = None
    ) -> bool:
        """Public entry point used by Tads3ParseCommand and save-triggered
        re-parses. Pass file_paths to re-parse only those files instead of the
        whole project (the server still re-runs project-wide preprocessing to
        keep macro/include state current, but skips symbol-extraction for
        everything else). Returns whether a request was actually sent — False
        if a parse was already in flight."""
        return self._send_parse(makefile, file_paths)

    def abort_parse(self) -> None:
        """Public entry point used by Tads3AbortParseCommand."""
        session = self.weaksession()
        if session:
            session.send_notification(Notification("symbolparsing/abort", {}))
        _state["parsing"] = False
        _state["single_file"] = False
        sublime.set_timeout(_refresh_status, 0)

    def _send_parse(
        self, makefile: str, file_paths: Optional[List[str]] = None
    ) -> bool:
        session = self.weaksession()
        if not session:
            return False
        if _state["parsing"]:
            # The server keeps parse state in module-level globals it clears at
            # the start of every request/parseDocuments call — overlapping
            # requests race on that shared state and one of them ends up
            # observing zero files ("No files to parse. Aborting operation").
            # Never let two be in flight at once.
            return False
        storage = _storage_path()
        os.makedirs(storage, exist_ok=True)
        _state["parsing"] = True
        _state["single_file"] = bool(file_paths and len(file_paths) == 1)
        params: Dict[str, Any] = {
            "globalStoragePath": storage,
            "makefileLocation": makefile,
        }
        if file_paths:
            params["filePaths"] = file_paths
        session.send_request_async(
            Request("request/parseDocuments", params),
            self._on_parse_response,
        )
        return True

    def _on_parse_response(self, response: Any) -> None:
        if response and response.get("error"):
            err = response["error"]
            _state["parsing"] = False
            sublime.set_timeout(
                lambda: sublime.error_message(
                    "[tads3tools] Parse failed: " + (err.get("message") or str(err))
                ),
                0,
            )

    # ─── Notification handlers ──────────────────────────────────────────────

    @staticmethod
    def _unwrap(params: Any) -> Any:
        """Unwrap JSON-RPC positional notation: params is [[...]], return [...]."""
        if isinstance(params, list) and params and isinstance(params[0], list):
            return params[0]
        return params

    def _on_processing(self, params: Any) -> None:
        p = self._unwrap(params)
        if not p:
            return
        _state["parsing"] = True
        _state["file"] = os.path.basename(p[0]) if len(p) > 0 else ""
        _state["tracker"] = p[1] if len(p) > 1 else 0
        _state["total"] = p[2] if len(p) > 2 else 0
        _state["pool_size"] = p[3] if len(p) > 3 else 1
        sublime.set_timeout(_refresh_status, 0)

    def _on_file_success(self, params: Any) -> None:
        p = self._unwrap(params)
        if not p:
            return
        _state["file"] = os.path.basename(p[0]) if len(p) > 0 else ""
        _state["tracker"] = p[1] if len(p) > 1 else 0
        _state["total"] = p[2] if len(p) > 2 else 0
        _state["pool_size"] = p[3] if len(p) > 3 else 1
        if _state["single_file"]:
            # The only completion signal a single-file parse ever gets —
            # symbolparsing/allfiles/success never fires for it.
            _state["parsing"] = False
            _state["single_file"] = False
            file_name = _state["file"] or "file"
            _state["file"] = ""
            sublime.set_timeout(_refresh_status, 0)
            sublime.set_timeout(
                lambda: sublime.status_message(
                    "[tads3tools] Parsed {}".format(file_name)
                ),
                0,
            )
            return
        sublime.set_timeout(_refresh_status, 0)

    def _on_file_failed(self, params: Any) -> None:
        # Terminal for a single-file parse — unlike symbolparsing/success
        # (progress during a multi-file parse, completion for a single-file
        # one), this only ever fires as the final word on a failed single-file
        # parse; multi-file failures go through symbolparsing/allfiles/failed.
        p = self._unwrap(params)
        file_name = os.path.basename(p[0]) if p and len(p) > 0 else "file"
        error = p[1] if p and len(p) > 1 else "unknown error"
        _state["parsing"] = False
        _state["single_file"] = False
        _state["file"] = ""
        sublime.set_timeout(_refresh_status, 0)
        sublime.set_timeout(
            lambda: sublime.error_message(
                "[tads3tools] Parse failed for {}: {}".format(file_name, error)
            ),
            0,
        )

    def _on_allfiles_success(self, params: Any) -> None:
        elapsed = params.get("elapsedTime", 0) if isinstance(params, dict) else 0
        total = _state["total"]
        _state["parsing"] = False
        _state["file"] = ""
        sublime.set_timeout(_refresh_status, 0)
        sublime.set_timeout(
            lambda: sublime.status_message(
                "[tads3tools] Parsed {} files in {} ms".format(total, elapsed)
            ),
            0,
        )

    def _on_allfiles_failed(self, params: Any) -> None:
        _state["parsing"] = False
        _state["file"] = ""
        sublime.set_timeout(_refresh_status, 0)
        if isinstance(params, dict):
            msg = params.get("error", "unknown error")
        elif isinstance(params, str):
            msg = params
        else:
            msg = "unknown error"
        sublime.set_timeout(
            lambda: sublime.error_message("[tads3tools] Parse failed: " + msg), 0
        )

    def _on_preprocessed_list(self, params: Any) -> None:
        if isinstance(params, list):
            _state["preprocessed"] = params

    def _on_makefile_kvmap(self, params: Any) -> None:
        if isinstance(params, dict):
            _state["using_adv3_lite"] = params.get("usingAdv3Lite", False)
            _state["makefile_kvs"] = params.get("makefileStructure", [])


# ─── Commands ──────────────────────────────────────────────────────────────────


def _plugin() -> Optional[LspTads3toolsPlugin]:
    return _plugin_instance() if _plugin_instance else None  # type: ignore[return-value]


class Tads3ParseCommand(LspTextCommand):
    """Send a full-project parse request to the tads3 language server."""

    session_name = SERVER_NAME

    def run(self, edit: sublime.Edit) -> None:
        buf_path = self.view.file_name() or ""
        makefile = _resolve_makefile(buf_path)
        if makefile:
            self._do_parse(makefile)
        else:
            self._prompt_makefile(buf_path)

    def _prompt_makefile(self, buf_path: str) -> None:
        paths = _find_makefiles(buf_path)
        if not paths:
            self.view.window().status_message("[tads3tools] No .t3m makefile found")
            return
        if len(paths) == 1:
            root = os.path.dirname(paths[0])
            _selected_makefiles[root] = paths[0]
            self._do_parse(paths[0])
            return
        window = self.view.window()
        window.show_quick_panel(
            [os.path.relpath(p) for p in paths],
            lambda idx: self._on_chosen(paths, idx),
            placeholder="Select TADS3 makefile",
        )

    def _on_chosen(self, paths: List[str], idx: int) -> None:
        if idx < 0:
            return
        root = os.path.dirname(paths[0])
        _selected_makefiles[root] = paths[idx]
        self._do_parse(paths[idx])

    def _do_parse(self, makefile: str) -> None:
        plugin = _plugin()
        if not plugin:
            sublime.status_message("[tads3tools] No active tads3 LSP session")
        elif not plugin.send_parse(makefile):
            sublime.status_message(
                "[tads3tools] A parse is already in progress — try again once it finishes."
            )


class Tads3AbortParseCommand(LspTextCommand):
    """Abort an in-progress parse."""

    session_name = SERVER_NAME

    def run(self, edit: sublime.Edit) -> None:
        plugin = _plugin()
        if plugin:
            plugin.abort_parse()


class Tads3BuildCommand(sublime_plugin.WindowCommand):
    """Compile with t3make; errors appear in the build panel."""

    def run(self) -> None:
        import shutil

        view = self.window.active_view()
        buf_path = view.file_name() if view else None
        if not buf_path:
            return
        makefile = _resolve_makefile(buf_path)
        if not makefile:
            self.window.status_message("[tads3tools] No .t3m makefile found")
            return
        if not shutil.which("t3make"):
            self.window.status_message("[tads3tools] t3make not found in PATH")
            return
        self.window.run_command(
            "exec",
            {
                "cmd": ["t3make", "-f", makefile],
                "working_dir": os.path.dirname(makefile),
                # t3make error format: file.t(42): error: message
                "file_regex": r"^(.+)\((\d+)\):\s+(.+)$",
                "syntax": "Packages/Default/Find Results.hidden-tmLanguage",
            },
        )


TERMINAL_INTERPRETERS = {"frob"}  # curses-based interpreters — need a real tty, not a build panel


class Tads3RunCommand(sublime_plugin.WindowCommand):
    """Launch the compiled game through the configured interpreter."""

    def run(self) -> None:
        view = self.window.active_view()
        buf_path = view.file_name() if view else None
        if not buf_path:
            return
        makefile = _resolve_makefile(buf_path)
        if not makefile:
            self.window.status_message("[tads3tools] No .t3m makefile found")
            return
        output = _find_output_file(makefile)
        if not output:
            self.window.status_message(
                "[tads3tools] No -o output line in " + os.path.basename(makefile)
            )
            return
        if not os.path.isfile(output):
            self.window.status_message(
                "[tads3tools] {} not found — run Tads3: Build first".format(
                    os.path.basename(output)
                )
            )
            return
        settings = sublime.load_settings("LSP-tads3tools.sublime-settings")
        interpreter = settings.get("interpreter", "frob")
        cmd = [interpreter, output]
        cwd = os.path.dirname(output)
        try:
            if interpreter in TERMINAL_INTERPRETERS:
                # Curses-based interpreters need a real tty; Sublime's exec panel
                # isn't one, which is why it fails with "Error opening terminal".
                self._launch_in_terminal(cmd, cwd)
            else:
                # GUI interpreters (qtads, htmltads, ...) — just spawn them directly.
                subprocess.Popen(cmd, cwd=cwd)
        except OSError as exc:
            sublime.error_message(
                "[tads3tools] Failed to launch {}: {}".format(interpreter, exc)
            )

    def _launch_in_terminal(self, cmd: List[str], cwd: str) -> None:
        system = platform.system()
        if system == "Darwin":
            self._launch_macos(cmd, cwd)
        elif system == "Linux":
            self._launch_linux(cmd, cwd)
        elif system == "Windows":
            self._launch_windows(cmd, cwd)
        else:
            sublime.error_message(
                "[tads3tools] Don't know how to open a terminal on this platform."
            )

    def _launch_macos(self, cmd: List[str], cwd: str) -> None:
        script = "#!/bin/sh\ncd {}\n{}\nread -n 1 -s -r -p 'Press any key to close...'\nrm -- \"$0\"\n".format(
            shlex.quote(cwd), " ".join(shlex.quote(c) for c in cmd)
        )
        fd, script_path = tempfile.mkstemp(suffix=".command")
        with os.fdopen(fd, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        subprocess.Popen(["open", "-a", "Terminal", script_path])

    def _launch_linux(self, cmd: List[str], cwd: str) -> None:
        shell_cmd = "cd {} && {}; read -n 1 -s -r -p 'Press any key to close...'".format(
            shlex.quote(cwd), " ".join(shlex.quote(c) for c in cmd)
        )
        gnome_terminal = shutil.which("gnome-terminal")
        if gnome_terminal:
            subprocess.Popen([gnome_terminal, "--", "sh", "-c", shell_cmd])
            return
        for terminal in ("x-terminal-emulator", "konsole", "xfce4-terminal", "xterm"):
            path = shutil.which(terminal)
            if path:
                subprocess.Popen([path, "-e", shell_cmd])
                return
        raise OSError(
            "No known terminal emulator found (tried gnome-terminal, "
            "x-terminal-emulator, konsole, xfce4-terminal, xterm)"
        )

    def _launch_windows(self, cmd: List[str], cwd: str) -> None:
        quoted = " ".join('"{}"'.format(c) for c in cmd)
        subprocess.Popen(["cmd", "/c", "start", "", "cmd", "/k", quoted], cwd=cwd)


class Tads3SelectMakefileCommand(sublime_plugin.WindowCommand):
    """Clear the cached makefile choice and re-prompt. Re-parse afterwards."""

    def run(self) -> None:
        view = self.window.active_view()
        buf_path = view.file_name() if view else None
        if not buf_path:
            return
        paths = _find_makefiles(buf_path)
        if not paths:
            self.window.status_message("[tads3tools] No .t3m makefile found")
            return
        root = os.path.dirname(paths[0])
        _selected_makefiles.pop(root, None)
        if len(paths) == 1:
            _selected_makefiles[root] = paths[0]
            self.window.status_message(
                "[tads3tools] Using {} — run Tads3: Parse to re-parse".format(
                    os.path.relpath(paths[0])
                )
            )
            return
        self.window.show_quick_panel(
            [os.path.relpath(p) for p in paths],
            lambda idx: self._on_chosen(paths, idx),
            placeholder="Select TADS3 makefile",
        )

    def _on_chosen(self, paths: List[str], idx: int) -> None:
        if idx < 0:
            return
        chosen = paths[idx]
        root = os.path.dirname(paths[0])
        _selected_makefiles[root] = chosen
        self.window.status_message(
            "[tads3tools] Using {} — run Tads3: Parse to re-parse".format(
                os.path.relpath(chosen)
            )
        )


class Tads3InstallServerCommand(sublime_plugin.WindowCommand):
    """Pick a server binary from the latest GitHub release and download it."""

    RELEASES_URL = "https://api.github.com/repos/toerob/vscode-tads3tools/releases/latest"
    ASSET_PREFIX = "vscode-tads3tools-server-"

    def run(self) -> None:
        self.window.status_message("[tads3tools] Checking latest release …")
        threading.Thread(target=self._fetch_assets, daemon=True).start()

    def _fetch_assets(self) -> None:
        request = urllib.request.Request(
            self.RELEASES_URL, headers={"User-Agent": "LSP-tads3tools"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                release = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            sublime.set_timeout(
                lambda: sublime.error_message(
                    "[tads3tools] Failed to fetch release info: " + str(exc)
                ),
                0,
            )
            return

        assets = [
            a
            for a in release.get("assets", [])
            if a.get("name", "").startswith(self.ASSET_PREFIX)
        ]
        if not assets:
            tag = release.get("tag_name", "latest")
            sublime.set_timeout(
                lambda: sublime.error_message(
                    "[tads3tools] No server binaries found in release {}.".format(tag)
                ),
                0,
            )
            return

        # Infer platform + CPU family so the matching asset can be preselected.
        info = _platform_info()
        recommended = (self.ASSET_PREFIX + info[0] + info[1]) if info else None
        assets.sort(key=lambda a: a["name"] != recommended)

        sublime.set_timeout(lambda: self._show_picker(assets, recommended), 0)

    def _show_picker(
        self, assets: List[Dict[str, Any]], recommended: Optional[str]
    ) -> None:
        items = []
        for a in assets:
            name = a["name"]
            size_mb = a.get("size", 0) / (1024 * 1024)
            note = "  ← recommended for your platform" if name == recommended else ""
            items.append([name + note, "{:.1f} MB".format(size_mb)])
        self.window.show_quick_panel(
            items,
            lambda idx: self._on_picked(assets, idx),
            placeholder="Select TADS3 server binary to install",
        )

    def _on_picked(self, assets: List[Dict[str, Any]], idx: int) -> None:
        if idx < 0:
            return
        asset = assets[idx]
        binary_name = asset["name"]
        bin_dir = _default_bin_dir()
        dest = os.path.join(bin_dir, binary_name)
        os.makedirs(bin_dir, exist_ok=True)
        self.window.status_message("[tads3tools] Downloading {} …".format(binary_name))
        threading.Thread(
            target=self._download,
            args=(asset["browser_download_url"], dest, binary_name),
            daemon=True,
        ).start()

    def _download(self, url: str, dest: str, binary_name: str) -> None:
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as exc:
            sublime.set_timeout(
                lambda: sublime.error_message(
                    "[tads3tools] Download failed: " + str(exc)
                ),
                0,
            )
            return

        if platform.system() != "Windows":
            os.chmod(dest, 0o755)

        sublime.set_timeout(lambda: self._finish(binary_name), 0)

    def _finish(self, binary_name: str) -> None:
        # additional_variables() re-resolves ${server_path} from disk on every
        # session (re)start, so restarting the session is enough to pick up the
        # freshly downloaded binary — no settings need editing.
        window = sublime.active_window()
        view = window.active_view() if window else None
        if view is not None:
            try:
                view.run_command("lsp_restart_server", {"config_name": SERVER_NAME})
            except Exception:
                pass
        sublime.message_dialog(
            "[tads3tools] Installed {}.\n\n"
            "If the language server was already running, it has been restarted. "
            "Otherwise, open a .t file (or restart Sublime Text) to start it.".format(
                binary_name
            )
        )


# ─── Event listener ────────────────────────────────────────────────────────────


class Tads3EventListener(sublime_plugin.EventListener):
    """Auto-select+parse a project's makefile on open, and re-parse on save."""

    def on_load_async(self, view: sublime.View) -> None:
        self._auto_parse(view)

    def on_activated_async(self, view: sublime.View) -> None:
        self._auto_parse(view)

    def on_post_save_async(self, view: sublime.View) -> None:
        buf_path = view.file_name()
        if not buf_path:
            return
        makefile = _resolve_makefile(buf_path)
        if not makefile:
            return
        root = os.path.dirname(makefile)
        if root not in _auto_parsed_roots:
            # No full baseline parse yet for this project — do that instead of a
            # single-file parse, which would leave everything else unindexed.
            _auto_parse_if_needed(buf_path)
            return
        self._debounced_reparse(makefile, root, buf_path)

    @staticmethod
    def _auto_parse(view: sublime.View) -> None:
        buf_path = view.file_name()
        if buf_path:
            _auto_parse_if_needed(buf_path)

    @staticmethod
    def _debounced_reparse(makefile: str, root: str, buf_path: str) -> None:
        # Collapse a burst of saves (e.g. Save All across a project) into a
        # single trailing request that re-parses every file saved in the
        # meantime — not a full project re-parse, and not just the last file.
        _pending_save_files.setdefault(root, set()).add(buf_path)
        generation = _save_reparse_generation.get(root, 0) + 1
        _save_reparse_generation[root] = generation

        def fire() -> None:
            if _save_reparse_generation.get(root) != generation:
                return  # superseded by a later save
            files = sorted(_pending_save_files.pop(root, set()))
            if not files:
                return
            plugin = _plugin()
            if not plugin:
                return
            if not plugin.send_parse(makefile, file_paths=files):
                # A parse was already in flight — put them back so the next
                # save (or its own debounce cycle) picks them up.
                _pending_save_files.setdefault(root, set()).update(files)

        sublime.set_timeout_async(fire, _SAVE_REPARSE_DEBOUNCE_MS)


# ─── Plugin lifecycle ──────────────────────────────────────────────────────────


def plugin_loaded() -> None:
    register_plugin(LspTads3toolsPlugin)


def plugin_unloaded() -> None:
    unregister_plugin(LspTads3toolsPlugin)
