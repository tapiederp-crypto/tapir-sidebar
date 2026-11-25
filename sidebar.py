#!/usr/bin/env python3
"""
chat-sidebar - Hyprland layer-shell sidebar for chatting with a local Ollama model.

* Opens/toggles with Ctrl+Shift+S (see ~/.config/hypr/custom.lua)
* Streams answers token-by-token straight from Ollama (http://127.0.0.1:11434)
* Keeps its own chat history, persisted across restarts
* Single instance: extra CLI args are sent as a message to the running instance

    sidebar.py                # show the sidebar
    sidebar.py hello there    # show it and send "hello there"
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell, Pango

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "chat-sidebar")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DATA_DIR = os.path.join(HOME, ".local", "share", "chat-sidebar")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
COLORS_PATH = os.path.join(HOME, ".config", "ml4w", "colors", "colors.json")

MAX_HISTORY = 200  # messages kept on disk / sent to the model
MONO = "JetBrainsMono Nerd Font"

DEFAULT_CONFIG = {
    "host": "http://127.0.0.1:11434",
    "model": "llama3.2:1b",
    "width": 420,
    "system_prompt": "You are a helpful assistant. Keep answers short and to the point.",
    "temperature": 0.7,
    "airdrop_dir": "",
}

DEFAULT_COLORS = {
    "background": "#121318",
    "surface_container": "#1d1e26",
    "surface_container_low": "#191a21",
    "surface_variant": "#45464f",
    "primary": "#b5c4ff",
    "on_primary": "#1c2d61",
    "on_surface": "#e3e1e9",
    "on_surface_variant": "#c6c6d0",
    "outline": "#8f909a",
    "outline_variant": "#45464f",
    "error": "#ffb4ab",
    "error_container": "#93000a",
    "on_error_container": "#ffdad6",
}


# ---------------------------------------------------------------- helpers

def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return fallback


def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def hex_rgb(color):
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    try:
        r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
        return r, g, b
    except Exception:
        return 18, 19, 24


def rgba(color, alpha=1.0):
    r, g, b = hex_rgb(color)
    return f"rgba({r},{g},{b},{alpha})"


def find_airdrop(configured=""):
    """Return the AirDropReceiver directory, or None when it is not installed."""
    candidates = []
    if configured:
        candidates.append(os.path.expanduser(configured))
    data_home = os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share"))
    candidates.append(os.path.join(data_home, "airdrop-linux", "AirDropReceiver"))
    candidates.append(os.path.join(HOME, "airdrop-linux", "AirDropReceiver"))
    for path in candidates:
        if os.path.isfile(os.path.join(path, "receive.py")):
            return path
    return None


# ---------------------------------------------------------------- markdown-lite

_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)


def _inline_md(text):
    t = GLib.markup_escape_text(text)
    t = re.sub(r"`([^`\n]+)`", f'<span face="{MONO}" background="#15161d">\\1</span>', t)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^(\s*)- ", r"\1" + "\u2022 ", t)
    return t


def md_to_pango(text, mono=MONO, bg2="#15161d"):
    out, last = [], 0
    for m in _CODE_BLOCK.finditer(text):
        out.append(_inline_md(text[last : m.start()]))
        body = m.group(1)
        if body.endswith("\n"):
            body = body[:-1]
        escaped = GLib.markup_escape_text(body)
        block = f'\n<span face="{mono}" background="{bg2}">{escaped}</span>\n'
        out.append(block)
        last = m.end()
    out.append(_inline_md(text[last:]))
    return "".join(out)


# ---------------------------------------------------------------- app

class ChatSidebar:
    def __init__(self):
        self.cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
        self.history = load_json(HISTORY_PATH, [])
        if not isinstance(self.history, list):
            self.history = []
        self.colors = {**DEFAULT_COLORS, **load_json(COLORS_PATH, {})}
        self.airdrop = find_airdrop(self.cfg.get("airdrop_dir", ""))
        self.models = []
        self.busy = False
        self.cancel = False
        self._resp = None
        self.pending = []
        self.win = None
        self.app = None
        self._empty_row = None
        self._answer_label = None

    # ---------------- ui construction

    def build(self):
        self._apply_css()

        self.win = Gtk.ApplicationWindow(application=self.app, title="AI Chat")
        self.win.set_default_size(int(self.cfg["width"]), -1)
        self.win.set_hide_on_close(False)

        Gtk4LayerShell.init_for_window(self.win)
        Gtk4LayerShell.set_namespace(self.win, "chat-sidebar")
        Gtk4LayerShell.set_layer(self.win, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_anchor(self.win, Gtk4LayerShell.Edge.RIGHT, True)
        Gtk4LayerShell.set_anchor(self.win, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self.win, Gtk4LayerShell.Edge.BOTTOM, True)
        Gtk4LayerShell.set_exclusive_zone(self.win, -1)
        try:  # keyboard only while the sidebar is visible: open -> type immediately
            Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        except AttributeError:
            Gtk4LayerShell.set_keyboard_interactivity(self.win, True)

        self.win.connect("close-request", self._on_close_request)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.win.set_child(root)

        root.append(self._build_header())
        root.append(self._build_airdrop_panel())
        root.append(self._build_messages())
        root.append(self._build_input())

        esc = Gtk.EventControllerKey()
        esc.connect("key-pressed", self._on_window_key)
        self.win.add_controller(esc)

        self._refresh_model_ui()
        self._set_busy_ui(False)

        threading.Thread(target=self._fetch_models, daemon=True).start()

    def _build_header(self):
        c = self.colors
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_start=12,
                      margin_end=8, margin_top=8, margin_bottom=8)
        box.add_css_class("header")

        title = Gtk.Label(label="AI Chat")
        title.add_css_class("title")
        box.append(title)

        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(14, 14)
        box.append(self.spinner)

        self.model_row = Gtk.DropDown.new_from_strings([self.cfg["model"]])
        self.model_row.set_hexpand(True)
        self.model_row.connect("notify::selected", self._on_model_changed)
        box.append(self.model_row)

        self.status = Gtk.Label(label="Ollama offline")
        self.status.add_css_class("err")
        self.status.set_visible(False)
        box.append(self.status)

        self.airdrop_btn = Gtk.ToggleButton(label="AirDrop")
        self.airdrop_btn.add_css_class("flat")
        self.airdrop_btn.add_css_class("dim-btn")
        self.airdrop_btn.connect("toggled", self._on_airdrop_toggled)
        box.append(self.airdrop_btn)

        clear = Gtk.Button(label="Clear")
        clear.add_css_class("flat")
        clear.add_css_class("dim-btn")
        clear.connect("clicked", lambda *_: self.clear_chat())
        box.append(clear)

        close = Gtk.Button(label="\u00d7")
        close.add_css_class("flat")
        close.connect("clicked", lambda *_: self.hide())
        box.append(close)

        return box

    def _build_airdrop_panel(self):
        self.airdrop_panel = Gtk.Revealer()
        self.airdrop_panel.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.airdrop_panel.set_reveal_child(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_start=12, margin_end=12, margin_top=2, margin_bottom=8)
        outer.add_css_class("airdrop-panel")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions = (
            ("Receive", self._on_airdrop_receive),
            ("Send\u2026", self._on_airdrop_send),
            ("Stop", self._on_airdrop_stop),
            ("Folder", self._on_airdrop_folder),
        )
        for label, handler in actions:
            button = Gtk.Button(label=label)
            button.add_css_class("airdrop-action")
            button.connect("clicked", handler)
            if self.airdrop is None:
                button.set_sensitive(False)
            row.append(button)
        outer.append(row)

        self.airdrop_status = Gtk.Label(xalign=0)
        self.airdrop_status.add_css_class("dim-label")
        self.airdrop_status.set_wrap(True)
        self.airdrop_status.set_visible(False)
        outer.append(self.airdrop_status)

        if self.airdrop is None:
            self._set_airdrop_status("AirDrop Linux was not found. Set airdrop_dir in config.json.")
        self.airdrop_panel.set_child(outer)
        return self.airdrop_panel

    def _build_messages(self):
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_vexpand(True)
        self.scroll.add_css_class("msgs")

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("msgs")
        self.listbox.set_vexpand(True)
        self.scroll.set_child(self.listbox)

        for msg in self.history:
            self._append_row(msg["role"], msg["content"], store=False)
        self._update_empty_state()
        return self.scroll

    def _build_input(self):
        c = self.colors
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                        margin_start=12, margin_end=12, margin_top=6, margin_bottom=10)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        card.add_css_class("input-card")

        self.tv = Gtk.TextView()
        self.tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.tv.set_vexpand(False)
        self.tv.set_valign(Gtk.Align.START)
        self.tv.set_top_margin(8)
        self.tv.set_bottom_margin(8)
        self.tv.set_left_margin(10)
        self.tv.set_right_margin(4)
        self.tv.get_buffer().connect("changed", self._on_buffer_changed)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", self._on_focus_change)
        focus.connect("leave", self._on_focus_change)
        self.tv.add_controller(focus)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_input_key)
        self.tv.add_controller(keys)

        tscroll = Gtk.ScrolledWindow()
        tscroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        tscroll.set_min_content_height(44)
        tscroll.set_max_content_height(132)
        tscroll.set_propagate_natural_height(True)
        tscroll.set_child(self.tv)

        overlay = Gtk.Overlay()
        overlay.set_child(tscroll)
        overlay.set_hexpand(True)
        self.placeholder = Gtk.Label(xalign=0)
        self.placeholder.add_css_class("placeholder")
        self.placeholder.set_margin_start(10)
        self.placeholder.set_valign(Gtk.Align.CENTER)
        self.placeholder.set_sensitive(False)
        overlay.add_overlay(self.placeholder)

        card.append(overlay)

        self.send_btn = Gtk.Button(label="Send")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.set_valign(Gtk.Align.CENTER)
        self.send_btn.connect("clicked", self._on_send_clicked)
        card.append(self.send_btn)

        outer.append(card)
        return outer

    # ---------------- css / theme

    def _apply_css(self):
        c = self.colors
        css = f"""
            window {{
                background-color: {rgba(c["background"], 0.93)};
                color: {c["on_surface"]};
            }}
            .header {{
                border-bottom-width: 1px;
                border-bottom-style: solid;
                border-bottom-color: {rgba(c["outline"], 0.35)};
            }}
            .title {{ font-size: 15px; font-weight: 800; }}
            button {{
                background-image: none;
                box-shadow: none;
                background-color: transparent;
                color: {c["on_surface_variant"]};
                border-color: transparent;
                border-width: 0px;
                border-radius: 9px;
                padding: 4px 9px;
                font-size: 13px;
            }}
            button:hover {{ background-color: {rgba(c["surface_variant"], 0.55)}; }}
            button.suggested-action {{
                background-image: none;
                box-shadow: none;
                background-color: {c["primary"]};
                color: {c["on_primary"]};
                font-weight: 700;
                padding: 6px 14px;
            }}
            button.suggested-action:hover {{ background-color: {rgba(c["primary"], 0.85)}; }}
            button.stop-btn {{
                background-color: {c["error_container"]};
                color: {c["on_error_container"]};
                font-weight: 700;
                padding: 6px 14px;
            }}
            dropdown {{
                background-color: {rgba(c["surface_variant"], 0.35)};
                border-radius: 9px;
                padding: 2px 4px;
            }}
            dropdown button {{
                background-image: none;
                background-color: transparent;
                color: {c["on_surface"]};
                padding: 3px 6px;
                font-size: 13px;
            }}
            dropdown button:hover {{ background-color: transparent; }}
            listview, list, row {{
                background-color: transparent;
            }}
            .bubble-user {{
                background-color: {c["primary"]};
                color: {c["on_primary"]};
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
                border-bottom-left-radius: 14px;
                border-bottom-right-radius: 4px;
                padding: 8px 12px;
            }}
            .bubble-assistant {{
                background-color: {rgba(c["surface_container"], 0.9)};
                color: {c["on_surface"]};
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
                border-bottom-left-radius: 4px;
                border-bottom-right-radius: 14px;
                padding: 8px 12px;
            }}
            .bubble-empty {{
                background-color: transparent;
                color: {c["on_surface_variant"]};
                padding: 8px 0px;
            }}
            .err {{
                color: {c["error"]};
                font-size: 13px;
                padding: 6px 12px;
            }}
            .placeholder {{
                color: {rgba(c["on_surface_variant"], 0.55)};
                font-size: 14px;
            }}
            .dim-label {{
                color: {c["on_surface_variant"]};
                font-size: 13px;
            }}
            .airdrop-panel {{
                border-bottom-width: 1px;
                border-bottom-style: solid;
                border-bottom-color: {rgba(c["outline"], 0.25)};
            }}
            button.airdrop-action {{
                background-color: {rgba(c["surface_variant"], 0.35)};
                color: {c["on_surface"]};
                padding: 5px 10px;
            }}
            button.airdrop-action:hover {{
                background-color: {rgba(c["surface_variant"], 0.6)};
            }}
            .input-card {{
                background-color: {rgba(c["surface_container_low"], 0.95)};
                border-width: 1px;
                border-style: solid;
                border-color: {rgba(c["outline_variant"], 0.9)};
                border-radius: 14px;
            }}
            textview {{
                background-color: transparent;
                color: {c["on_surface"]};
                caret-color: {c["primary"]};
                font-size: 14px;
            }}
            viewport, scrolledwindow {{
                background-color: transparent;
            }}
            button:disabled {{
                background-image: none;
                box-shadow: none;
                background-color: {rgba(c["surface_variant"], 0.4)};
                color: {rgba(c["on_surface_variant"], 0.6)};
            }}
            button.suggested-action:disabled {{
                background-image: none;
                box-shadow: none;
                background-color: {rgba(c["primary"], 0.45)};
                color: {c["on_primary"]};
            }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # ---------------- window / visibility

    def show(self):
        if self.win is None:
            self.build()
        self.win.present()
        GLib.idle_add(lambda: (self.tv.grab_focus(), False)[1])

    def hide(self):
        if self.win is not None:
            self.win.set_visible(False)

    def toggle(self):
        if self.win is None or not self.win.get_visible():
            self.show()
        else:
            self.hide()

    def _on_close_request(self, *_):
        self.hide()
        return True  # keep the process alive; next toggle reuses it

    def _on_window_key(self, _ctrl, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def _on_input_key(self, _ctrl, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                return False  # newline
            self._on_send_clicked(None)
            return True
        return False

    def _on_focus_change(self, *_):
        self._update_placeholder()

    def _on_buffer_changed(self, *_):
        self._update_placeholder()

    def _update_placeholder(self):
        empty = self._input_text() == ""
        focused = self.tv.has_focus()
        self.placeholder.set_visible(empty and not focused)

    def _input_text(self):
        buf = self.tv.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _clear_input(self):
        self.tv.get_buffer().set_text("")

    # ---------------- airdrop

    def _on_airdrop_toggled(self, button):
        self.airdrop_panel.set_reveal_child(button.get_active())

    def _set_airdrop_status(self, text):
        self.airdrop_status.set_text(text)
        self.airdrop_status.set_visible(bool(text))

    def _close_airdrop_menu(self):
        self.airdrop_btn.set_active(False)

    def _on_airdrop_receive(self, *_):
        self._close_airdrop_menu()
        self._run_airdrop("receive.py", hide=True)

    def _on_airdrop_send(self, *_):
        self._close_airdrop_menu()
        self._run_airdrop("send-dialog.py", "--start-radio", hide=True)

    def _on_airdrop_stop(self, *_):
        self._close_airdrop_menu()
        if self._run_airdrop("stop.py"):
            self._set_airdrop_status("Stopping AirDrop\u2026")

    def _on_airdrop_folder(self, *_):
        self._close_airdrop_menu()
        folder = self._incoming_directory()
        try:
            os.makedirs(folder, exist_ok=True)
            subprocess.Popen(["xdg-open", folder], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            self._set_airdrop_status(f"Could not open {folder}: {exc}")
        else:
            self._set_airdrop_status("")

    def _run_airdrop(self, script, *args, hide=False):
        if not self.airdrop:
            self._set_airdrop_status("AirDrop Linux was not found. Set airdrop_dir in config.json.")
            return False
        path = os.path.join(self.airdrop, script)
        if not os.path.isfile(path):
            self._set_airdrop_status(f"Missing {script} in {self.airdrop}.")
            return False
        env = os.environ.copy()
        env.pop("LD_PRELOAD", None)  # the layer-shell preload is only for this GTK process
        if hide:
            self.hide()
        try:
            subprocess.Popen([sys.executable, path, *args], cwd=self.airdrop, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            if hide:
                self.show()
            self._set_airdrop_status(f"Could not start AirDrop: {exc}")
            return False
        return True

    def _incoming_directory(self):
        configured = os.environ.get("AIRDROP_INCOMING_DIR")
        if configured:
            return os.path.expanduser(configured)
        downloads = os.path.join(HOME, "Downloads")
        try:
            result = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True,
                                    text=True, timeout=2)
            if result.returncode == 0 and os.path.isabs(result.stdout.strip()):
                downloads = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return os.path.join(downloads, "AirDrop")

    # ---------------- messages ui

    def _append_row(self, role, text, store=True):
        if store:
            self.history.append({"role": role, "content": text})
            self._trim_history()
            self._save_history()
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_margin_top(5)
        inner.set_margin_bottom(5)
        inner.set_margin_start(14)
        inner.set_margin_end(14)

        label = Gtk.Label(xalign=0)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_selectable(role == "assistant")
        label.set_max_width_chars(38 if role == "user" else 46)
        label.set_markup(md_to_pango(text) if role == "assistant" else GLib.markup_escape_text(text))
        label.add_css_class("bubble-user" if role == "user" else "bubble-assistant")

        if role == "user":
            inner.set_halign(Gtk.Align.END)
        inner.append(label)
        row.set_child(inner)
        self.listbox.append(row)
        self._update_empty_state()
        self._scroll_to_end(force=True)
        return label

    def _placeholder_assistant_row(self):
        """Row shown while the model is thinking / streaming."""
        return self._append_row("assistant", "\u2026", store=False)

    def _update_assistant(self, label, text):
        label.set_markup(md_to_pango(text if text else "\u2026"))
        self._scroll_to_end()

    def _remove_label(self, label):
        row = label.get_parent().get_parent()
        self.listbox.remove(row)
        self._update_empty_state()

    def _show_error(self, message):
        label = Gtk.Label(label=f"Error: {message}", xalign=0)
        label.set_wrap(True)
        label.add_css_class("err")
        self.listbox.append(label)
        self._scroll_to_end(force=True)

    def _update_empty_state(self):
        if self._empty_row is not None:
            self.listbox.remove(self._empty_row)
            self._empty_row = None
        if self.listbox.get_first_child() is None:
            label = Gtk.Label(
                label=f"Chat with {self.cfg['model']}\nAsk anything \u2014 Enter to send",
                xalign=0.5,
            )
            label.set_justify(Gtk.Justification.CENTER)
            label.add_css_class("dim-label")
            label.set_margin_top(24)
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.set_child(label)
            self._empty_row = row
            self.listbox.append(row)

    def _scroll_to_end(self, force=False):
        adj = self.scroll.get_vadjustment()
        near_bottom = (adj.get_upper() - adj.get_page_size() - adj.get_value()) < 80
        if force or near_bottom:
            GLib.idle_add(lambda: (adj.set_value(max(0, adj.get_upper() - adj.get_page_size())), False)[1])

    # ---------------- sending / streaming

    def _on_send_clicked(self, *_):
        if self.busy:
            self.cancel = True
            resp = self._resp
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            return

        text = self._input_text().strip()
        if not text:
            return
        self._clear_input()
        self._dispatch(text)

    def send_message(self, text):
        """Queue (or send) a message from outside the input box."""
        text = (text or "").strip()
        if not text:
            return
        self.pending.append(text)
        self._drain_pending()

    def _drain_pending(self):
        if self.busy or not self.pending:
            return
        text = self.pending.pop(0)
        self._append_row("user", text, store=True)
        self._start_generation()

    def _dispatch(self, text):
        self._append_row("user", text, store=True)
        self._start_generation()

    def _start_generation(self):
        self.busy = True
        self.cancel = False
        self._set_busy_ui(True)
        self._answer_label = self._placeholder_assistant_row()
        threading.Thread(target=self._stream_worker, daemon=True).start()

    def _stream_worker(self):
        cfg = self.cfg
        messages = []
        if cfg.get("system_prompt"):
            messages.append({"role": "system", "content": cfg["system_prompt"]})
        messages.extend({"role": m["role"], "content": m["content"]} for m in self.history)

        payload = json.dumps(
            {
                "model": cfg["model"],
                "messages": messages,
                "stream": True,
                "options": {"temperature": float(cfg.get("temperature", 0.7))},
            }
        ).encode("utf-8")

        acc = ""
        resp = None
        error = None
        try:
            req = urllib.request.Request(
                cfg["host"] + "/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=180)
            self._resp = resp
            for raw in resp:
                if self.cancel:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    raise RuntimeError(obj["error"])
                delta = (obj.get("message") or {}).get("content", "")
                if delta:
                    acc += delta
                    GLib.idle_add(self._update_assistant, self._answer_label, acc)
                if obj.get("done"):
                    break
        except Exception as exc:  # network errors, closed connection, ...
            if not self.cancel:
                error = str(exc) or exc.__class__.__name__
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
                self._resp = None
            GLib.idle_add(self._finish, acc, error)

    def _finish(self, acc, error):
        self.busy = False
        self.cancel = False

        if acc and acc.strip():
            # the streaming row already shows the final text — persist it instead
            # of appending a second, identical bubble
            self.history.append({"role": "assistant", "content": acc})
            self._trim_history()
            self._save_history()
            self._update_assistant(self._answer_label, acc)
        else:
            # nothing generated (error right away or cancelled immediately)
            self._remove_label(self._answer_label)

        if error:
            self._show_error(error)

        self._set_busy_ui(False)
        self._drain_pending()

    def _set_busy_ui(self, busy):
        if busy:
            self.send_btn.set_label("Stop")
            self.send_btn.add_css_class("stop-btn")
            self.send_btn.remove_css_class("suggested-action")
            self.spinner.start()
        else:
            self.send_btn.set_label("Send")
            self.send_btn.remove_css_class("stop-btn")
            self.send_btn.add_css_class("suggested-action")
            self.spinner.stop()

    # ---------------- models

    def _fetch_models(self):
        names = []
        try:
            with urllib.request.urlopen(self.cfg["host"] + "/api/tags", timeout=4) as resp:
                data = json.load(resp)
            names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        except Exception:
            names = []
        GLib.idle_add(self._apply_models, names)

    def _apply_models(self, names):
        self.models = names
        current = self.cfg["model"]
        items = [current] + [n for n in names if n != current]
        self.model_row.set_model(Gtk.StringList.new(items))
        offline = not names
        self.status.set_visible(offline)
        if offline:
            self.send_btn.set_sensitive(False)
        elif not self.busy:
            self.send_btn.set_sensitive(True)

    def _on_model_changed(self, row, *_):
        idx = row.get_selected()
        model = row.get_model()
        if model is None:
            return
        name = model.get_string(idx)
        if name and name != self.cfg["model"]:
            self.cfg["model"] = name
            save_json(CONFIG_PATH, self.cfg)
            self._refresh_model_ui()

    def _refresh_model_ui(self):
        if not self.busy and self.models:
            self.send_btn.set_sensitive(True)

    # ---------------- misc

    def clear_chat(self):
        if self.busy:
            self.cancel = True
            resp = self._resp
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
        self.history = []
        self._save_history()
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt
        self._update_empty_state()

    def _trim_history(self):
        if len(self.history) > MAX_HISTORY:
            del self.history[: len(self.history) - MAX_HISTORY]

    def _save_history(self):
        save_json(HISTORY_PATH, self.history)


# ---------------------------------------------------------------- entry point

def main():
    config = ChatSidebar()
    app = Gtk.Application(
        application_id="org.ml4w.chat-sidebar",
        flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
    )
    config.app = app

    def on_command_line(application, command_line):
        argv = command_line.get_arguments()[1:]
        config.show()
        if argv:
            config.send_message(" ".join(argv))
        return 0

    app.connect("command-line", on_command_line)

    def on_sigusr1():
        config.toggle()
        return True  # keep the source

    def on_sigterm():
        app.quit()
        return False

    try:
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except ImportError:
        add_signal = GLib.unix_signal_add
    add_signal(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, on_sigusr1)
    add_signal(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_sigterm)

    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
