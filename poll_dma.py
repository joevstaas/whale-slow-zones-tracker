"""
Poll NOAA/NEFSC Dynamic Management Areas API and build a historical dataset.

The NOAA API only exposes currently active Right Whale Slow Zones / DMAs.
Once a zone expires or is cancelled, it disappears from the API. This script
polls the API on a schedule, detects new and disappeared zones, and builds
a persistent history with activation/deactivation timestamps.

The history can be exported in the Ocean Data Platform (ODP) GeoJSON schema
for integration with the WSC Whale Chart dataset.

Usage:
    python poll_dma.py                  # Run a single poll
    python poll_dma.py --history        # Print history table
    python poll_dma.py --export-geojson # Export history as ODP-compatible GeoJSON
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

API_URL = (
    "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/"
    "NEFSC_Dynamic_Management_Areas/FeatureServer/0/query"
    "?where=1%3D1&outFields=*&f=geojson&outSR=4326"
)

DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "dma_history.json"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
EXPORT_FILE = DATA_DIR / "dma_history.geojson"


def fetch_active_zones() -> dict:
    """Fetch currently active zones from the NEFSC ArcGIS API."""
    req = Request(API_URL, headers={"User-Agent": "whale-slow-zones-tracker/1.0"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def load_history() -> dict:
    """Load existing history or create empty structure."""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {"zones": {}, "polls": []}


def save_history(history: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def save_snapshot(geojson: dict, now: str):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = SNAPSHOT_DIR / f"dma_{now.replace(':', '-')}.geojson"
    with open(filename, "w") as f:
        json.dump(geojson, f, indent=2)
    return filename


def poll():
    """Run a single poll: fetch active zones and update history."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Polling at {now}...")

    geojson = fetch_active_zones()
    features = geojson.get("features", [])
    snapshot_file = save_snapshot(geojson, now)

    history = load_history()
    active_ids = set()

    for feat in features:
        props = feat["properties"]
        zone_id = str(props["ID"])
        active_ids.add(zone_id)

        if zone_id not in history["zones"]:
            # New zone — first time we've seen it
            history["zones"][zone_id] = {
                "id": props["ID"],
                "name": props["NAME"],
                "trigger_type": props["TRIGGERTYPE"],
                "expiration_date": props["EXPDATE"],
                "first_seen": now,
                "last_seen": now,
                "cancelled": props["CANCELLED"],
                "comments": props["COMMENTS"],
                "geometry": feat["geometry"],
                "status": "active",
                "gone_since": None,
            }
            print(f"  NEW: {props['NAME']} (ID {zone_id}, trigger={props['TRIGGERTYPE']}, expires={props['EXPDATE']})")
        else:
            # Existing zone — update last_seen and any changed fields
            zone = history["zones"][zone_id]
            zone["last_seen"] = now
            zone["cancelled"] = props["CANCELLED"]
            zone["comments"] = props["COMMENTS"]
            zone["expiration_date"] = props["EXPDATE"]
            if zone["status"] != "active":
                # Zone reappeared (unlikely but handle it)
                zone["status"] = "active"
                zone["gone_since"] = None
                print(f"  REACTIVATED: {zone['name']} (ID {zone_id})")

    # Detect zones that disappeared since last poll
    for zone_id, zone in history["zones"].items():
        if zone_id not in active_ids and zone["status"] == "active":
            zone["status"] = "expired_or_cancelled"
            zone["gone_since"] = now
            print(f"  GONE: {zone['name']} (ID {zone_id}, was active since {zone['first_seen']})")

    history["polls"].append({
        "timestamp": now,
        "active_zone_ids": sorted(active_ids),
    })

    save_history(history)

    active = sum(1 for z in history["zones"].values() if z["status"] == "active")
    total = len(history["zones"])
    print(f"Done. {active} active, {total} total zones tracked.")


def print_history():
    """Print a summary of all tracked zones."""
    history = load_history()
    if not history["zones"]:
        print("No history yet. Run a poll first.")
        return

    print(f"{'ID':>6}  {'Name':<30}  {'Trigger':<8}  {'Expires':<22}  {'Status':<22}  {'First Seen':<26}  {'Last Seen':<26}")
    print("-" * 160)

    for zone_id in sorted(history["zones"], key=lambda k: int(k)):
        z = history["zones"][zone_id]
        print(
            f"{z['id']:>6}  {z['name']:<30}  {z['trigger_type'] or '?':<8}  "
            f"{z['expiration_date'] or 'N/A':<22}  {z['status']:<22}  "
            f"{z['first_seen']:<26}  {z['last_seen']:<26}"
        )

    print(f"\nTotal polls: {len(history['polls'])}")
    if history["polls"]:
        print(f"First poll: {history['polls'][0]['timestamp']}")
        print(f"Last poll:  {history['polls'][-1]['timestamp']}")


def export_geojson():
    """Export history as ODP-compatible GeoJSON (WSC Whale Chart schema)."""
    history = load_history()
    if not history["zones"]:
        print("No history yet. Run a poll first.")
        return

    features = []
    for zone_id, z in sorted(history["zones"].items(), key=lambda kv: int(kv[0])):
        # Parse expiration date to ISO format for the 'end' field
        end_date = None
        if z["expiration_date"]:
            try:
                dt = datetime.strptime(z["expiration_date"], "%d-%b-%Y %H:%M:%S")
                end_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                end_date = z["expiration_date"]

        # Map trigger_type to human-readable activation_trigger
        trigger_map = {"v": "v", "a": "a"}
        activation_trigger = trigger_map.get(z["trigger_type"], z["trigger_type"])

        feature = {
            "type": "Feature",
            "geometry": z["geometry"],
            "properties": {
                "fid": f"NEFSC-{z['id']}",
                "name": z["name"],
                "measure_type": "NEFSC Right Whale Slow Zone",
                "mandatory": False,
                "description": z["comments"] or None,
                "country": None,
                "seasonality": None,
                "start": z["first_seen"][:10] if z["first_seen"] else None,
                "end": end_date,
                "speed_limit_kn": 10.0,
                "dynamic": True,
                "activation_trigger": activation_trigger,
                "distance_min_m": None,
                "vessel_len_min_m": None,
                "taxons": "",
                "source_url": (
                    "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/"
                    "NEFSC_Dynamic_Management_Areas/FeatureServer/0"
                ),
                "s127_feature": "RestrictedAreaRegulatory",
                "s127_feature_subcat": "speed restricted",
                "provided_coordinates": "",
                "geometry_notes": (
                    f"NEFSC ID: {z['id']}; "
                    f"Cancelled: {z['cancelled'] or 'None'}; "
                    f"Status: {z['status']}; "
                    f"First seen: {z['first_seen']}; "
                    f"Last seen: {z['last_seen']}"
                    + (f"; Gone since: {z['gone_since']}" if z['gone_since'] else "")
                ),
                "updated": z["last_seen"],
            },
        }
        features.append(feature)

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(EXPORT_FILE, "w") as f:
        json.dump(geojson, f, indent=2)

    print(f"Exported {len(features)} zones to {EXPORT_FILE}")


if __name__ == "__main__":
    if "--history" in sys.argv:
        print_history()
    elif "--export-geojson" in sys.argv:
        export_geojson()
    else:
        poll()
