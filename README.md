# minicord

Minimal Discord REST API library for Python, with a `uv`-first workflow and guidance for free-threaded CPython (`3.13t`).

## What this is

`minicord` is a tiny starting point for Discord REST integrations.
It currently includes:

- A small HTTP client wrapper
- One example API method: `GET /gateway/bot`
- A smoke test setup with `pytest`

## Requirements

- `uv` installed
- A free-threaded Python build (CPython `3.13t`) for experiments with Python free-threading

## Quickstart (uv + free-threaded Python)

```bash
uv python install 3.13t
uv venv --python 3.13t .venv
source .venv/bin/activate
uv sync
```

Run tests:

```bash
uv run pytest
```

## Usage

```python
from minicord import MinicordClient

client = MinicordClient(token="YOUR_BOT_TOKEN")

# Calls Discord's GET /gateway/bot endpoint.
# See: https://discord.com/developers/docs/topics/gateway#get-gateway-bot
result = client.get_gateway_bot()
print(result)
```

## Notes on free-threading

- Free-threaded Python is still evolving; package compatibility varies.
- This library itself is pure Python and intended to be simple to experiment with under `3.13t`.
