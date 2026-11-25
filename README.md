# chat-sidebar

A sidebar for chatting with your local Ollama model (`llama3.2:1b`), styled to match
your ml4w theme. Built as a Hyprland layer-shell panel (GTK4 + gtk4-layer-shell), like
the sidebars in end-4's dots-hyprland.

## Setup

Requires Ollama running locally, Python 3 with PyGObject (GTK4), `gtk4-layer-shell`,
and (optionally) Hyprland for the keybind and blur rule.

```sh
mkdir -p ~/.config/chat-sidebar ~/.local/share/chat-sidebar
cp sidebar.py toggle.sh ~/.config/chat-sidebar/
cp config.example.json ~/.config/chat-sidebar/config.json
chmod +x ~/.config/chat-sidebar/toggle.sh
```

Edit `~/.config/chat-sidebar/config.json` to change the host, model, width, system
prompt, or temperature. The model dropdown lists the models currently installed in
Ollama (`ollama list`); restart the sidebar after installing a new model to refresh it.
Chat history is stored at `~/.local/share/chat-sidebar/history.json` and is not part
of this repository.

## Usage

| Key | Action |
| --- | --- |
| `Ctrl+Shift+S` | Open / close the sidebar |
| `Enter` | Send the message |
| `Shift+Enter` | New line |
| `Esc` | Close the sidebar |

Answers stream in token by token. **Clear** wipes the chat history, the dropdown picks
the model, and **Send** turns into **Stop** while the model is generating.

From a terminal:

```sh
~/.config/chat-sidebar/toggle.sh                 # toggle
~/.config/chat-sidebar/toggle.sh hello there     # open and send "hello there"
```

## AirDrop

Click **AirDrop** in the header to open the AirDrop actions:

| Action | Effect |
| --- | --- |
| `Receive` | Starts the AirDrop receiver (asks for your Linux password) |
| `Send…` | Opens the AirDrop send dialog with device discovery |
| `Stop` | Stops the running AirDrop receiver |
| `Folder` | Opens `Downloads/AirDrop` |

The sidebar hides before opening a dialog so the AirDrop window or password prompt
gets keyboard focus. Reopen it with `Ctrl+Shift+S`. The AirDrop location is read from
`airdrop_dir` in `config.json`; when empty, the installed app in
`~/.local/share/airdrop-linux/AirDropReceiver` is used automatically.

## Files

- `sidebar.py` – the sidebar (GTK4, layer-shell, streams from Ollama)
- `toggle.sh` – start/toggle helper (bound in `~/.config/hypr/custom.lua`)
- `config.json` – host, model, width, system prompt, temperature
- history: `~/.local/share/chat-sidebar/history.json`

The keybind and the blur rule live in `~/.config/hypr/custom.lua`, which ml4w loads
after its own config, so they survive dotfile updates.

Note: because Hyprland consumes `Ctrl+Shift+S` globally, apps no longer receive it
(for example *Save All* in VS Code) while this binding exists.

Theme colors are read from `~/.config/ml4w/colors/colors.json` on every start, so the
sidebar follows your current matugen palette.
