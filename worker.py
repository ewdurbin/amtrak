#!/usr/bin/env python3
"""
Amtrak data polling worker
Fetches train and station data from Amtrak API and stores in database using SQLAlchemy
"""

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, UTC

import requests
from fake_useragent import UserAgent
from sqlalchemy import and_, or_, text

# Import the amtrak module
from amtrak import decrypt_data, parse_crypto, parse_stations, parse_trains
from models import init_db, get_session, Train, Station, Metadata

ua = UserAgent()

# Configuration
TRAIN_POLL_INTERVAL = int(os.environ.get("TRAIN_POLL_INTERVAL", "10"))  # seconds
STATION_POLL_INTERVAL = int(os.environ.get("STATION_POLL_INTERVAL", "120"))  # seconds
WEBSHARE_API_KEY = os.environ.get("WEBSHARE_API_KEY")

# Global proxy list
PROXY_LIST = []
PROXY_INDEX = 0


def fetch_proxy_list():
    """Fetch proxy list from Webshare.io API"""
    global PROXY_LIST

    if not WEBSHARE_API_KEY:
        return False

    try:
        headers = {"Authorization": f"Token {WEBSHARE_API_KEY}"}

        response = requests.get(
            "https://proxy.webshare.io/api/v2/proxy/list/",
            headers=headers,
            params={
                "mode": "direct",
                "page": 1,
                "page_size": 100,  # Get up to 100 proxies
            },
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            PROXY_LIST = data.get("results", [])
            print(f"Fetched {len(PROXY_LIST)} proxies from Webshare.io")
            return True
        else:
            print(f"Failed to fetch proxies: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"Error fetching proxy list: {e}")
        return False


def get_next_proxy():
    """Get the next proxy from the list using round-robin"""
    global PROXY_INDEX

    if not PROXY_LIST:
        return None, None

    proxy = PROXY_LIST[PROXY_INDEX]
    PROXY_INDEX = (PROXY_INDEX + 1) % len(PROXY_LIST)

    # Format proxy for requests library
    proxy_url = (
        f"http://{proxy['username']}:{proxy['password']}@"
        f"{proxy['proxy_address']}:{proxy['port']}"
    )
    proxy_dict = {"http": proxy_url, "https": proxy_url}

    # Return both the proxy config and info for logging
    proxy_info = (
        f"{proxy['proxy_address']}:{proxy['port']} ({proxy.get('country_code', 'XX')})"
    )
    return proxy_dict, proxy_info


def fetch_trains_data():
    """Fetch train data from API"""
    try:
        headers = {"User-Agent": ua.random}
        proxies, proxy_info = get_next_proxy()

        if proxy_info:
            print(f"Fetching trains via proxy: {proxy_info}")

        # Get crypto keys
        routes_resp = requests.get(
            "https://maps.amtrak.com/rttl/js/RoutesList.json",
            headers=headers,
            proxies=proxies,
            timeout=30,
        )
        routes = routes_resp.json()

        crypto_resp = requests.get(
            "https://maps.amtrak.com/rttl/js/RoutesList.v.json",
            headers=headers,
            proxies=proxies,
            timeout=30,
        )
        crypto_data = crypto_resp.json()

        public_key, salt, iv = parse_crypto(routes, crypto_data)

        # Get encrypted train data
        response = requests.get(
            "https://maps.amtrak.com/services/MapDataService/trains/getTrainsData",
            headers=headers,
            proxies=proxies,
            timeout=30,
        )
        response.raise_for_status()

        # Decrypt and parse
        decrypted_data = decrypt_data(response.content, public_key, salt, iv)
        trains_data = json.loads(decrypted_data)
        parsed_trains = parse_trains(trains_data)

        if proxy_info:
            print(f"Successfully fetched trains via proxy: {proxy_info}")

        return parsed_trains
    except Exception as e:
        if proxy_info:
            print(f"Error fetching trains via proxy {proxy_info}: {e}")
        else:
            print(f"Error fetching trains: {e}")
        traceback.print_exc()
        return None


def fetch_stations_data():
    """Fetch station data from API"""
    try:
        headers = {"User-Agent": ua.random}
        proxies, proxy_info = get_next_proxy()

        if proxy_info:
            print(f"Fetching stations via proxy: {proxy_info}")

        # Get crypto keys
        routes_resp = requests.get(
            "https://maps.amtrak.com/rttl/js/RoutesList.json",
            headers=headers,
            proxies=proxies,
            timeout=30,
        )
        routes = routes_resp.json()

        crypto_resp = requests.get(
            "https://maps.amtrak.com/rttl/js/RoutesList.v.json",
            headers=headers,
            proxies=proxies,
            timeout=30,
        )
        crypto_data = crypto_resp.json()

        public_key, salt, iv = parse_crypto(routes, crypto_data)

        # Get encrypted station data
        response = requests.get(
            "https://maps.amtrak.com/services/MapDataService/stations/trainStations",
            headers=headers,
            proxies=proxies,
            timeout=30,
        )
        response.raise_for_status()

        # Decrypt and parse
        decrypted_data = decrypt_data(response.content, public_key, salt, iv)
        stations_data = json.loads(decrypted_data)
        parsed_stations = parse_stations(stations_data)

        if proxy_info:
            print(f"Successfully fetched stations via proxy: {proxy_info}")

        return parsed_stations
    except Exception as e:
        if proxy_info:
            print(f"Error fetching stations via proxy {proxy_info}: {e}")
        else:
            print(f"Error fetching stations: {e}")
        traceback.print_exc()
        return None


def serialize_for_json(obj):
    """Convert datetime objects to ISO format strings for JSON serialization"""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def update_trains_in_db(trains_data, stations_data=None):
    """Update trains data in database"""
    if not trains_data:
        return

    session = get_session()

    try:
        # Don't delete old trains anymore - we want to keep historic data

        # Track which trains we've seen in this update
        seen_train_ids = set()

        # Insert/update train data
        for train_number, train_list in trains_data.items():
            for train in train_list:
                train_id = str(train.get("id", ""))
                route_name = train.get("route_name", "")
                departure_date = train.get("departure_date")
                train_state = train.get("state", "")  # Predeparture, Active, Completed

                # Track this train as seen
                seen_train_ids.add((train_number, train_id))

                # Filter stations to only relevant ones for this train
                relevant_stations = None
                if stations_data and train.get("stations"):
                    relevant_stations = {}
                    for station_code in train["stations"].keys():
                        if station_code in stations_data:
                            relevant_stations[station_code] = stations_data[
                                station_code
                            ]

                # Convert datetime objects to ISO format strings before JSON serialization
                train_serializable = train.copy()
                for key in [
                    "departure_date",
                    "last_update",
                    "last_fetched",
                    "scheduled_departure",
                ]:
                    if key in train_serializable and train_serializable[key]:
                        train_serializable[key] = serialize_for_json(
                            train_serializable[key]
                        )

                # Convert station datetime objects
                if "stations" in train_serializable:
                    for station_code, station in train_serializable["stations"].items():
                        for category in ["scheduled", "estimated", "actual"]:
                            if category in station and station[category]:
                                for field in ["arrival", "departure"]:
                                    if (
                                        field in station[category]
                                        and station[category][field]
                                    ):
                                        station[category][field] = serialize_for_json(
                                            station[category][field]
                                        )

                # Check if train exists
                existing_train = (
                    session.query(Train)
                    .filter(
                        and_(
                            Train.train_number == train_number,
                            Train.train_id == train_id,
                        )
                    )
                    .first()
                )

                if existing_train:
                    # Update existing train
                    existing_train.route_name = route_name
                    existing_train.departure_date = departure_date
                    existing_train.data = train_serializable
                    existing_train.updated_at = datetime.now(UTC)

                    # Update station snapshot when train state changes
                    if train_state != existing_train.train_state:
                        existing_train.stations_snapshot = relevant_stations

                    existing_train.train_state = train_state
                else:
                    # Create new train with station snapshot
                    new_train = Train(
                        train_number=train_number,
                        train_id=train_id,
                        route_name=route_name,
                        departure_date=departure_date,
                        train_state=train_state,
                        stations_snapshot=relevant_stations,
                        data=train_serializable,
                    )
                    session.add(new_train)

        # Clean up trains no longer in the response but marked as "Active" or "Predeparture"
        # Find all trains where either train_state OR data->state is "Active" or "Predeparture"

        # Query for trains that have Active or Predeparture in either field
        # Use database-agnostic JSON extraction
        active_trains = (
            session.query(Train)
            .filter(
                or_(
                    Train.train_state.in_(["Active", "Predeparture"]),
                    text(
                        "(data->>'state') IN ('Active', 'Predeparture')"
                    ),  # Works for both PostgreSQL and SQLite with JSON1
                )
            )
            .all()
        )

        for train in active_trains:
            # If train is not in current API response, mark it as completed
            if (train.train_number, train.train_id) not in seen_train_ids:
                # Check if we need to update anything
                needs_update = False

                if train.train_state != "Completed":
                    print(
                        f"Marking train {train.train_number} "
                        f"(ID: {train.train_id}, was: {train.train_state}) as Completed - "
                        "no longer in API response"
                    )
                    train.train_state = "Completed"
                    needs_update = True

                # Also update the state in the JSON data field if needed
                if train.data and isinstance(train.data, dict):
                    json_state = train.data.get("state")
                    if json_state != "Completed":
                        if (
                            not needs_update
                        ):  # Only print if we didn't already print above
                            print(
                                f"Fixing JSON state for train {train.train_number} "
                                f"(ID: {train.train_id}) from '{json_state}' to 'Completed'"
                            )
                        # Create a new dict to ensure SQLAlchemy detects the change
                        updated_data = dict(train.data)
                        updated_data["state"] = "Completed"
                        train.data = updated_data
                        needs_update = True

                if needs_update:
                    train.updated_at = datetime.now(UTC)

        # Update metadata
        last_update = (
            session.query(Metadata).filter(Metadata.key == "last_train_update").first()
        )
        if last_update:
            last_update.value = datetime.now().isoformat()
            last_update.updated_at = datetime.now(UTC)
        else:
            session.add(
                Metadata(key="last_train_update", value=datetime.now().isoformat())
            )

        session.commit()
        print(f"Updated {len(trains_data)} train records at {datetime.now()}")
    except Exception as e:
        print(f"Error updating trains in database: {e}")
        traceback.print_exc()
        session.rollback()
    finally:
        session.close()


def update_stations_in_db(stations_data):
    """Update stations data in database"""
    if not stations_data:
        return

    session = get_session()

    try:
        # Insert/update station data
        for code, station in stations_data.items():
            name = station.get("station_name", "")
            geometry = station.get("geometry", {})
            coordinates = geometry.get("coordinates", [0, 0])
            lon = coordinates[0] if len(coordinates) > 0 else 0
            lat = coordinates[1] if len(coordinates) > 1 else 0

            # Check if station exists
            existing_station = (
                session.query(Station).filter(Station.code == code).first()
            )

            if existing_station:
                # Update existing station
                existing_station.name = name
                existing_station.lat = lat
                existing_station.lon = lon
                existing_station.data = station
                existing_station.updated_at = datetime.now(UTC)
            else:
                # Create new station
                new_station = Station(
                    code=code, name=name, lat=lat, lon=lon, data=station
                )
                session.add(new_station)

        # Update metadata
        last_update = (
            session.query(Metadata)
            .filter(Metadata.key == "last_station_update")
            .first()
        )
        if last_update:
            last_update.value = datetime.now().isoformat()
            last_update.updated_at = datetime.now(UTC)
        else:
            session.add(
                Metadata(key="last_station_update", value=datetime.now().isoformat())
            )

        session.commit()
        print(f"Updated {len(stations_data)} station records at {datetime.now()}")
    except Exception as e:
        print(f"Error updating stations in database: {e}")
        traceback.print_exc()
        session.rollback()
    finally:
        session.close()


def run_worker():
    """Main worker loop"""
    print("Starting Amtrak data worker...")
    print(f"Train poll interval: {TRAIN_POLL_INTERVAL}s")
    print(f"Station poll interval: {STATION_POLL_INTERVAL}s")

    # Initialize database
    engine = init_db()
    print(f"Database initialized: {engine.url}")

    # Fetch initial proxy list if API key is configured
    if WEBSHARE_API_KEY:
        print("Fetching proxy list from Webshare.io...")
        fetch_proxy_list()
    else:
        print("No WEBSHARE_API_KEY configured, running without proxies")

    # Set up signal handlers for graceful shutdown
    shutdown = False

    def signal_handler(signum, frame):
        nonlocal shutdown
        print(f"\nReceived signal {signum}, shutting down gracefully...")
        shutdown = True

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    last_train_update = 0
    last_station_update = 0
    last_proxy_refresh = time.time()
    cached_stations_data = None  # Keep stations in memory for train updates
    proxy_refresh_interval = 3600  # Refresh proxy list every hour

    while not shutdown:
        try:
            current_time = time.time()

            # Refresh proxy list periodically (only if API key is configured)
            if (
                WEBSHARE_API_KEY
                and current_time - last_proxy_refresh >= proxy_refresh_interval
            ):
                print("Refreshing proxy list...")
                fetch_proxy_list()
                last_proxy_refresh = current_time

            # Update stations first if needed, so we have fresh data for trains
            if current_time - last_station_update >= STATION_POLL_INTERVAL:
                print("Fetching station data...")
                stations_data = fetch_stations_data()
                if stations_data:
                    cached_stations_data = stations_data
                    update_stations_in_db(stations_data)
                last_station_update = current_time

            # Update trains
            if current_time - last_train_update >= TRAIN_POLL_INTERVAL:
                print("Fetching train data...")
                trains_data = fetch_trains_data()
                if trains_data:
                    update_trains_in_db(trains_data, cached_stations_data)
                last_train_update = current_time

            # Sleep for 1 second before checking again
            time.sleep(1)

        except Exception as e:
            print(f"Unexpected error in worker loop: {e}")
            traceback.print_exc()
            time.sleep(5)  # Wait before retrying

    print("Worker shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    run_worker()
