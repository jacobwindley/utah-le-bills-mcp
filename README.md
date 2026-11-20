# le-bills-mcp

An MCP server for Utah State Legislature data from [le.utah.gov](https://le.utah.gov/data/developer.htm):
bills, bill text, votes, fiscal notes, legislators, committees and meetings. With a developer
token it also covers the Utah Code, Constitution, legislative rules and meeting agendas.

It is built to keep LLM context small. Every tool has a character budget, long results page
with `next_offset`, and bill text stays on the server until you ask for a specific piece.

## Setup

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

From the project folder, install the dependencies:

```sh
uv sync
```

Optionally, copy `.env.example` to `.env` and add an `LE_UTAH_TOKEN` to enable the
Code, Constitution, Rules and agenda tools:

```sh
# macOS/Linux
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env

# Windows (Command Prompt)
copy .env.example .env
```

### Claude Code

Replace the path with where you cloned the project:

```sh
# macOS/Linux
claude mcp add le-bills -- uv --directory /path/to/le-bills-mcp run le-bills-mcp

# Windows (forward slashes work and avoid quoting issues)
claude mcp add le-bills -- uv --directory C:/path/to/le-bills-mcp run le-bills-mcp
```

### Claude Desktop app

1. **Find the full path to `uv`.** Desktop apps don't always see your shell's `PATH`, so a bare
   `uv` may fail to start.
   - Windows (PowerShell): `(Get-Command uv).Source`, e.g. `C:\Users\you\.local\bin\uv.exe`
   - macOS/Linux: `which uv`, e.g. `/Users/you/.local/bin/uv`
2. **Open the config file.** In Claude Desktop, go to **Settings → Developer → Edit Config**.
   This opens `claude_desktop_config.json` and creates it if it doesn't exist. If you'd rather
   open it yourself, it lives at:
   - Windows (installer): `%APPDATA%\Claude\claude_desktop_config.json`
   - Windows (Microsoft Store): `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
3. **Add the server** under `mcpServers`. If the file already has other servers, add the
   `le-bills` entry next to them rather than replacing the file.

   macOS/Linux:

   ```json
   {
     "mcpServers": {
       "le-bills": {
         "command": "/Users/you/.local/bin/uv",
         "args": ["--directory", "/path/to/le-bills-mcp", "run", "le-bills-mcp"]
       }
     }
   }
   ```

   Windows:

   ```json
   {
     "mcpServers": {
       "le-bills": {
         "command": "C:\\Users\\you\\.local\\bin\\uv.exe",
         "args": ["--directory", "C:\\path\\to\\le-bills-mcp", "run", "le-bills-mcp"]
       }
     }
   }
   ```

   In JSON, Windows paths need doubled backslashes (`C:\\path`), or you can use forward
   slashes (`C:/path`).
4. **Optional: settings.** The server reads `.env` from the project folder, so a token you put
   there is picked up automatically. To keep settings in the app config instead, add an `env`
   block to the entry:

   ```json
   "env": {
     "LE_UTAH_TOKEN": "your-token",
     "LE_MAX_RESPONSE_CHARS": "4000"
   }
   ```

5. **Fully quit and reopen Claude Desktop.** Closing the window isn't enough. On Windows, quit
   from the system tray icon; on macOS, use **Claude → Quit**.
6. **Check that it loaded.** In a new chat, open the tools menu (the sliders icon in the message
   box) and look for `le-bills`. Then try asking: *"What happened to HB 1 in the 2026 Utah
   general session?"*

**If it doesn't show up:**
- Go to **Settings → Developer**. A failed server is listed there with its error.
- Logs are in the `logs` folder next to the config file (`mcp-server-le-bills.log`). On macOS
  that is `~/Library/Logs/Claude/`.
- Run the same command in a terminal to see any error directly:
  `uv --directory /path/to/le-bills-mcp run le-bills-mcp` (on Windows, use
  `C:/path/to/le-bills-mcp`). If it's working, it starts and then
  waits silently for input; press Ctrl+C to exit.
- Common causes are a wrong `uv` or project path, single backslashes in the JSON, or a missing
  comma between servers.

### Settings

Set these in `.env` in the project folder, or in your MCP client's `env` block.

| Variable | Default | Purpose |
|---|---|---|
| `LE_UTAH_TOKEN` | unset | GLEN developer token. Enables `get_utah_code`, `get_constitution`, `get_legislative_rule`, `get_meeting_agenda`. |
| `LE_MAX_RESPONSE_CHARS` | `4000` | Default per-response character budget (500–20000). |
| `LE_CACHE_DIR` | unset (memory only) | Optional directory to save the bill search index in, so it survives restarts (about 1.2 MB per session). |

## Tools

**Bills**
- `get_bill`: metadata and summary, with `detail` set to brief, standard or full.
- `get_bill_history`: actions, votes and floor debates. By default it returns recent actions plus all votes.
- `get_bill_documents`: text versions, fiscal notes and committee reports, as URLs.
- `get_fiscal_note`: the fiscal note as plain text, paged.
- `search_bills`: keyword search over titles, subjects and summaries, with sponsor, subject and status filters.
- `refresh_bill_index`: updates the search index or reports its progress.
- `list_bills`, `list_passed_bills`, `list_bill_requests`.

**Bill text workspace**
- `load_bill` loads a bill's text into server memory and returns only a table of contents.
- `read_bill_text` reads a section, or pages through the whole bill. Amendments show as `[+inserted+]` and `[-deleted-]`.
- `find_in_bill` returns matching snippets with their section ids.
- `list_loaded_bills` and `unload_bill` manage the workspace, which holds up to 10 bills (LRU).
- Resources: `bill://{session}/{bill}` and `bill://{session}/{bill}/section/{id}`.

**People and meetings**
- `list_legislators`, `get_legislator`, `list_committees`, `get_committee`, `get_meeting_calendar`.

Sessions are written as `2026` or `2026GS` for a general session and `2025S1` for a special
session. Omit the session to use the current general session. Bill numbers accept `HB 1`,
`H.B. 1`, `hb0001` and `SJR3`.

## Search index and rate limits

le.utah.gov's bill list has no titles, and the site rate-limits bursts of requests (HTTP 429).
The first `search_bills` call for a session therefore does three things:

1. Seeds the index right away with titles from `passedlist.json`.
2. Fetches every bill's JSON in the background, one about every 0.6 s, so the first build takes about 10 minutes. It backs off on 429.
3. Refreshes it incrementally after that, refetching only bills whose `updatetime` changed.

By default the index is kept in memory only, so nothing is written to disk, and it is rebuilt
after each server restart. Set `LE_CACHE_DIR` to save it so later restarts skip the 10-minute
build. (A developer token doesn't help here: GLEN's bill list has no titles either.)

Searches during the build return partial results, and an `index.building` flag says so.

## Development

```sh
uv run pytest                                 # unit + fixture-based tool tests (offline)
uv run mcp dev src/le_bills_mcp/server.py     # MCP Inspector
```

`tests/test_server.py` checks that every tool stays within `max_chars`.

## License

MIT. See [LICENSE](LICENSE).
