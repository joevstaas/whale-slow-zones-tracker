# whale-slow-zones-tracker

Automated tracker for NOAA's dynamic Right Whale Slow Zones (Dynamic Management Areas) along the US East Coast.

## Why this exists

NOAA's [NEFSC ArcGIS API](https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/NEFSC_Dynamic_Management_Areas/FeatureServer/0) only exposes **currently active** slow zones. Once a zone expires or is cancelled, it disappears from the API with no public archive.

This repo polls the API daily via GitHub Actions, detects when zones are activated and deactivated, and builds a persistent history — creating the historical record that doesn't otherwise exist.

## What are Dynamic Management Areas?

When North Atlantic right whales are detected off the US East Coast — either through **visual sightings** or **acoustic monitoring** — NOAA establishes temporary slow zones requesting vessels to travel at 10 knots or less. These zones:

- Are **voluntary** (unlike the seasonal mandatory speed restrictions)
- Last approximately **15 days** per activation
- Can appear **anywhere** along the East Coast, at **any time of year**
- Shift location as whales move

See the [WSC Whale Chart](https://www.worldshipping.org/whales) for the full context of global whale protection measures for shipping.

## Data

### `data/dma_history.json`
The primary dataset. Each zone entry contains:

| Field | Description |
|---|---|
| `id` | NEFSC zone ID (sequential, e.g. 3909) |
| `name` | Location description (e.g. "34nm SW Nantucket MA") |
| `trigger_type` | `"v"` = visual sighting, `"a"` = acoustic detection |
| `expiration_date` | NOAA's stated expiration for the zone |
| `first_seen` | UTC timestamp when our poller first detected the zone (proxy for activation) |
| `last_seen` | UTC timestamp of the last poll where the zone was still active |
| `gone_since` | UTC timestamp of the first poll where the zone was no longer active |
| `status` | `"active"` or `"expired_or_cancelled"` |
| `cancelled` | NOAA cancellation flag (if zone was ended early) |
| `geometry` | GeoJSON polygon of the zone boundary |

The file also contains a `polls` array logging every poll timestamp and which zones were active at that time.

### `data/dma_history.geojson`
The same data exported as a GeoJSON FeatureCollection, using the **Ocean Data Platform (ODP) schema** from the WSC Whale Chart dataset. This makes it directly mergeable with the full Whale Chart GeoJSON.

ODP schema fields mapped:

| ODP Field | Source |
|---|---|
| `fid` | `NEFSC-{id}` |
| `name` | Zone name from API |
| `measure_type` | `"NEFSC Right Whale Slow Zone"` |
| `mandatory` | `false` |
| `start` | `first_seen` date (when poller detected activation) |
| `end` | `expiration_date` from NOAA |
| `speed_limit_kn` | `10.0` |
| `dynamic` | `true` |
| `activation_trigger` | `"a"` (acoustic) or `"v"` (visual) |
| `s127_feature` | `"RestrictedAreaRegulatory"` |
| `s127_feature_subcat` | `"speed restricted"` |
| `source_url` | NEFSC ArcGIS FeatureServer URL |
| `geometry_notes` | NEFSC ID, cancellation status, first/last seen timestamps |
| `updated` | `last_seen` timestamp |

### `data/dma_history.csv`
The same data as a flat CSV — renders nicely as a table on GitHub. Columns:

| Column | Description |
|---|---|
| `id` | NEFSC zone ID |
| `name` | Location description |
| `trigger_type` | `v` (visual) or `a` (acoustic) |
| `status` | `active` or `expired_or_cancelled` |
| `first_seen` | When the poller first detected the zone |
| `last_seen` | Last poll where the zone was still active |
| `gone_since` | First poll where the zone was no longer active |
| `expiration_date` | NOAA's stated expiration |
| `cancelled` | NOAA cancellation flag |
| `speed_limit_kn` | Speed limit (always 10 kn) |
| `bbox_south/north/west/east` | Bounding box of the zone polygon |
| `comments` | NOAA comments |

### `data/snapshots/`
Raw GeoJSON responses from each poll, timestamped. Useful for auditing and debugging.

## How it works

A GitHub Actions workflow runs daily at 06:00 UTC:

1. Fetches all currently active zones from the NOAA API
2. Compares against known zones in `dma_history.json`
3. Records new zones (with `first_seen` timestamp) and marks disappeared zones (with `gone_since` timestamp)
4. Exports the updated ODP-compatible GeoJSON and CSV
5. Commits and pushes changes

You can also trigger a poll manually from the Actions tab.

## Local usage

```bash
# Run a single poll
python poll_dma.py

# View history
python poll_dma.py --history

# Export ODP-compatible GeoJSON
python poll_dma.py --export-geojson

# Export CSV
python poll_dma.py --export-csv
```

No dependencies beyond Python 3.10+ standard library.

## Limitations

- **No backfill**: history starts from the first poll. Zones that expired before tracking began are lost.
- **Activation time is approximate**: `first_seen` reflects when the poller detected the zone, not the exact NOAA activation time. With daily polling, this is accurate to ~24 hours.
- **US East Coast only**: this tracks NEFSC Dynamic Management Areas. Canadian dynamic zones (Gulf of St. Lawrence) are activated via NAVWARN bulletins and are not available through this API.
