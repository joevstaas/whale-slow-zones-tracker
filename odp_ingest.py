"""
Ingest whale slow zone data into the Ocean Data Platform (ODP).

Reads the local history built by poll_dma.py and pushes it to the ODP dataset:
  1. Uploads the GeoJSON file (full state snapshot)
  2. Appends tabular zone records to the dataset table

Each cron run appends a new snapshot of all zones, building a time-series
that can be queried by the 'updated' column for point-in-time views.

Usage:
    python odp_ingest.py                # Upload file + append table rows
    python odp_ingest.py --file-only    # Only upload GeoJSON file
    python odp_ingest.py --table-only   # Only append tabular data
"""

import os
import sys
from datetime import datetime

import pyarrow as pa
import requests
from shapely.geometry import shape
from shapely import wkt

from odp.client import Client
from odp.catalog_v2 import get_dataset_meta_by_name

from poll_dma import EXPORT_FILE, load_history

COLLECTION_UID = "9e9f0fc4-3c9c-41a4-a5be-8771fcd5ec84"
DATASET_NAME = "NEFSC Whale Slow Zones"


def get_client() -> Client:
    """Create an authenticated ODP client using an API key."""
    api_key = os.environ.get("ODP_API_KEY")
    if not api_key:
        print("ERROR: ODP_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)
    return Client(api_key=api_key)


def get_or_create_dataset(client: Client) -> str:
    """Look up the dataset by name, or create it under the collection."""
    existing = get_dataset_meta_by_name(client, DATASET_NAME)
    if existing:
        print(f"Found existing dataset: {existing.id}")
        return existing.id

    res = client._request(
        requests.Request(
            method="POST",
            url=client.base_url + "/api/catalog/v2/datasets",
            json={
                "name": DATASET_NAME,
                "description": "NEFSC Right Whale Dynamic Management Areas — daily snapshots of active slow zones.",
            },
        ),
        retry=False,
    )
    res.raise_for_status()
    dataset_id = res.json()["id"]

    res2 = client._request(
        requests.Request(
            method="POST",
            url=client.base_url + f"/api/catalog/v2/data-collections/{COLLECTION_UID}/datasets/{dataset_id}",
        ),
        retry=False,
    )
    res2.raise_for_status()

    print(f"Created dataset {dataset_id} in collection {COLLECTION_UID}")
    return dataset_id


def geojson_geometry_to_wkt(geometry: dict) -> str | None:
    """Convert a GeoJSON geometry dict to a WKT string."""
    if not geometry:
        return None
    geom = shape(geometry)
    return wkt.dumps(geom)


def build_table_schema() -> pa.Schema:
    """Build the PyArrow schema for the ODP tabular storage."""
    return pa.schema([
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("name", pa.string()),
        pa.field("measure_type", pa.string()),
        pa.field("mandatory", pa.bool_()),
        pa.field("description", pa.string()),
        pa.field("start", pa.string()),
        pa.field("end", pa.string()),
        pa.field("speed_limit_kn", pa.float64()),
        pa.field("dynamic", pa.bool_()),
        pa.field("activation_trigger", pa.string()),
        pa.field("s127_feature", pa.string()),
        pa.field("s127_feature_subcat", pa.string()),
        pa.field("source_url", pa.string()),
        pa.field("geometry_notes", pa.string()),
        pa.field("updated", pa.string()),
        pa.field("status", pa.string()),
        pa.field("geo", pa.string(), metadata={"isGeometry": "1", "index": "1"}),
    ])


def build_zone_rows(history: dict) -> list[dict]:
    """Convert zone history into flat dicts matching the table schema."""
    rows = []
    for zone_id, z in sorted(history["zones"].items(), key=lambda kv: int(kv[0])):
        # Parse expiration date to ISO format
        end_date = None
        if z["expiration_date"]:
            try:
                dt = datetime.strptime(z["expiration_date"], "%d-%b-%Y %H:%M:%S")
                end_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                end_date = z["expiration_date"]

        geometry_notes = (
            f"NEFSC ID: {z['id']}; "
            f"Cancelled: {z['cancelled'] or 'None'}; "
            f"Status: {z['status']}; "
            f"First seen: {z['first_seen']}; "
            f"Last seen: {z['last_seen']}"
            + (f"; Gone since: {z['gone_since']}" if z["gone_since"] else "")
        )

        rows.append({
            "source_id": f"NEFSC-{z['id']}",
            "name": z["name"],
            "measure_type": "NEFSC Right Whale Slow Zone",
            "mandatory": False,
            "description": z["comments"] or None,
            "start": z["first_seen"][:10] if z["first_seen"] else None,
            "end": end_date,
            "speed_limit_kn": 10.0,
            "dynamic": True,
            "activation_trigger": z["trigger_type"],
            "s127_feature": "RestrictedAreaRegulatory",
            "s127_feature_subcat": "speed restricted",
            "source_url": (
                "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/"
                "NEFSC_Dynamic_Management_Areas/FeatureServer/0"
            ),
            "geometry_notes": geometry_notes,
            "updated": z["last_seen"],
            "status": z["status"],
            "geo": geojson_geometry_to_wkt(z.get("geometry")),
        })

    return rows


def upload_geojson(ds):
    """Upload the GeoJSON file to the ODP dataset."""
    if not EXPORT_FILE.exists():
        print(f"GeoJSON file not found: {EXPORT_FILE}")
        print("Run 'python poll_dma.py --export-geojson' first.")
        sys.exit(1)

    with open(EXPORT_FILE, "rb") as f:
        fid = ds.files.upload("dma_history.geojson", f)

    file_size = EXPORT_FILE.stat().st_size
    print(f"Uploaded GeoJSON ({file_size} bytes), file ID: {fid}")


def sync_table(ds):
    """Append zone records to the ODP dataset table."""
    history = load_history()

    if not history["zones"]:
        print("No history data. Run poll_dma.py first.")
        return

    rows = build_zone_rows(history)

    # Ensure table exists (idempotent — catches error if already created)
    try:
        ds.table.create(build_table_schema())
        print("Created new table with schema.")
    except Exception:
        pass  # Table already exists

    with ds as tx:
        tx.insert(rows)

    print(f"Appended {len(rows)} zone records to ODP table.")


def main():
    client = get_client()
    dataset_id = get_or_create_dataset(client)
    ds = client.dataset(dataset_id)

    if "--file-only" in sys.argv:
        upload_geojson(ds)
    elif "--table-only" in sys.argv:
        sync_table(ds)
    else:
        upload_geojson(ds)
        sync_table(ds)


if __name__ == "__main__":
    main()
