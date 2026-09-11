# OpenBusData MCP Server

mcp-name: io.github.AndrewAubury/openbusdata

MCP server for the [UK Bus Open Data Service](https://data.bus-data.dft.gov.uk) with rich
timetable parsing, stop search, route discovery, journey planning and real-time bus tracking.

## Features

- **Live API tools** — query timetables, fares, disruptions, cancellations and real-time bus locations
- **Stop search** — fuzzy text search across every bus stop in the UK
- **Route finder** — discover all routes serving a pair of stops
- **Journey planner** — "get to X by Y o'clock" with support for direct and chained multi-leg journeys
- **Live tracking** — see exactly where buses are right now

## Installation

### Option 1: via `uvx` (recommended — no install needed)

```bash
uvx openbusdata-mcp
```

### Option 2: via `pip`

```bash
pip install openbusdata-mcp
```

## Configuration

Set your Bus Open Data Service API key as an environment variable:

```bash
export OPENBUS_API_KEY="your-api-key-here"
```

Get a free key at [data.bus-data.dft.gov.uk](https://data.bus-data.dft.gov.uk).

## Usage

### With `uvx` (recommended)

Add to your MCP client (Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "openbusdata": {
      "command": "uvx",
      "args": ["openbusdata-mcp"],
      "env": {
        "OPENBUS_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### With `pip` install

```json
{
  "mcpServers": {
    "openbusdata": {
      "command": "openbusdata-mcp",
      "env": {
        "OPENBUS_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

## Development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install in editable mode with test dependencies
pip install -e ".[dev]"

# Run the test suite
pytest

# Run server
python -m openbusdata_mcp.server
```

## License

MIT
