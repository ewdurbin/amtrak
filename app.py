import os
from collections import defaultdict
from datetime import datetime, timedelta, UTC
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp_jinja2
import jinja2
import orjson
import sentry_sdk
from aiohttp import web

from models import get_session, Train, Station, Metadata

routes = web.RouteTableDef()

if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(dsn=os.environ.get("SENTRY_DSN"))


def json_dumps(*a, **kw):
    return orjson.dumps(*a, **kw).decode()


os.environ["TZ"] = "UTC"


def get_trains_from_db():
    """Fetch active trains or recently completed trains from database"""
    session = get_session()

    try:
        # Get active trains or trains completed in the last 6 hours
        six_hours_ago = datetime.now(UTC) - timedelta(hours=6)
        trains_query = (
            session.query(Train)
            .filter(
                (Train.train_state.in_(["Predeparture", "Active"]))
                | (
                    (Train.train_state == "Completed")
                    & (Train.updated_at > six_hours_ago)
                )
            )
            .order_by(Train.train_number, Train.departure_date)
        )

        trains = defaultdict(list)
        for train_record in trains_query:
            train_number = train_record.train_number
            train_data = train_record.data

            # Include stations snapshot if available
            if train_record.stations_snapshot:
                train_data["stations_snapshot"] = train_record.stations_snapshot

            # Convert ISO date strings back to datetime objects
            # For now, just parse them as-is with their UTC offset
            # We'll fix the timezone display after processing stations
            for key in [
                "departure_date",
                "last_update",
                "last_fetched",
                "scheduled_departure",
            ]:
                if train_data.get(key) and isinstance(train_data[key], str):
                    try:
                        train_data[key] = datetime.fromisoformat(train_data[key])
                    except (ValueError, TypeError):
                        pass  # Keep as is if conversion fails

            # Convert station dates and fix timezone to show proper abbreviations
            if train_data.get("stations"):
                for station_code, station in train_data["stations"].items():
                    # Get the station's timezone
                    tz_name = station.get("tz", "America/New_York")
                    try:
                        tz = ZoneInfo(tz_name)
                    except Exception:
                        tz = None

                    for category in ["scheduled", "estimated", "actual"]:
                        if station.get(category):
                            for field in ["arrival", "departure"]:
                                if station[category].get(field) and isinstance(
                                    station[category][field], str
                                ):
                                    try:
                                        # Parse the ISO string
                                        dt = datetime.fromisoformat(
                                            station[category][field]
                                        )
                                        # If we have a proper timezone, convert to it
                                        if tz and dt:
                                            # Preserves actual time but gives proper tz name
                                            dt = dt.astimezone(tz)
                                        station[category][field] = dt
                                    except (ValueError, TypeError):
                                        pass  # Keep as is if conversion fails

            # Now fix train-level datetimes to use proper timezone
            if train_data.get("stations"):
                # For departure_date and scheduled_departure, use first station's timezone
                first_station = next(iter(train_data["stations"].values()), {})
                first_tz_name = first_station.get("tz")

                if first_tz_name:
                    try:
                        first_tz = ZoneInfo(first_tz_name)
                        for key in ["departure_date", "scheduled_departure"]:
                            if train_data.get(key) and hasattr(
                                train_data[key], "astimezone"
                            ):
                                train_data[key] = train_data[key].astimezone(first_tz)
                    except Exception:
                        pass

                # For last_update and last_fetched, use the timezone of the train's current location
                # Find the last station with an actual arrival/departure time
                current_station_tz = None
                for station_code, station in train_data["stations"].items():
                    # Check if this station has actual times (meaning train has been there)
                    if station.get("actual"):
                        if station["actual"].get("arrival") or station["actual"].get(
                            "departure"
                        ):
                            # This station has actual times, so train has been here
                            current_station_tz = station.get("tz")

                # If we found a current location, use its timezone; otherwise use first station
                tz_to_use = current_station_tz if current_station_tz else first_tz_name

                if tz_to_use:
                    try:
                        tz = ZoneInfo(tz_to_use)
                        for key in ["last_update", "last_fetched"]:
                            if train_data.get(key) and hasattr(
                                train_data[key], "astimezone"
                            ):
                                train_data[key] = train_data[key].astimezone(tz)
                    except Exception:
                        pass

            trains[train_number].append(train_data)

        return dict(trains)
    finally:
        session.close()


def get_stations_from_db():
    """Fetch all stations from database"""
    session = get_session()

    try:
        stations_query = session.query(Station).all()

        stations = {}
        for station_record in stations_query:
            code = station_record.code
            station_data = station_record.data
            stations[code] = station_data

        return stations
    finally:
        session.close()


def get_last_update_times():
    """Get last update times from metadata"""
    session = get_session()

    try:
        metadata_query = session.query(Metadata).filter(
            Metadata.key.in_(["last_train_update", "last_station_update"])
        )

        metadata = {}
        for record in metadata_query:
            metadata[record.key] = record.value

        return metadata
    finally:
        session.close()


@routes.get("/")
@aiohttp_jinja2.template("index.jinja2")
async def index(request):
    return {}


@routes.get("/about")
@aiohttp_jinja2.template("about.jinja2")
async def about(request):
    return {}


@routes.get("/trains")
@aiohttp_jinja2.template("trains.jinja2")
async def trains(request):
    data = get_trains_from_db()
    return {"trains": data}


@routes.get("/trains/json")
async def trains_json(request):
    data = get_trains_from_db()
    return web.json_response(data, dumps=json_dumps)


@routes.get("/trains/{train_number}/json")
@routes.get("/trains/{train_number}/{train_id}/json")
async def train_json(request):
    data = get_trains_from_db()
    train_number = request.match_info["train_number"]
    train_id = request.match_info.get("train_id")

    if train_number in data.keys():
        if train_id is None:
            return web.json_response(data[train_number], dumps=json_dumps)
        else:
            for train in data[train_number]:
                try:
                    _train_id = int(train_id)
                    if train["id"] == _train_id:
                        return web.json_response(train, dumps=json_dumps)
                except ValueError:
                    if train["departure_date"].strftime("%Y-%m-%d") == train_id:
                        return web.json_response(train, dumps=json_dumps)

    return web.json_response({"message": "Train not found"}, status=404)


@routes.get("/trains/{train_number}/_partial")
@routes.get("/trains/{train_number}/{train_id}/_partial")
@aiohttp_jinja2.template("train_partial.jinja2")
async def train_partial(request):
    data = get_trains_from_db()
    train_number = request.match_info["train_number"]
    train_id = request.match_info.get("train_id")

    if train_number in data.keys():
        if train_id is None:
            selected_train = data[train_number][0]
            # Use stations_snapshot if available, otherwise fall back to fetching stations
            stations = selected_train.get("stations_snapshot") or get_stations_from_db()
            return {
                "stations": stations,
                "train": selected_train,
                "train_ids": [t["id"] for t in data[train_number]],
            }
        else:
            for train in data[train_number]:
                try:
                    _train_id = int(train_id)
                    if train["id"] == _train_id:
                        # Use stations_snapshot if available, else fetch from DB
                        stations = (
                            train.get("stations_snapshot") or get_stations_from_db()
                        )
                        return {
                            "stations": stations,
                            "train": train,
                            "train_ids": [
                                (t["id"], t["departure_date"])
                                for t in data[train_number]
                            ],
                        }
                except ValueError:
                    if train["departure_date"].strftime("%Y-%m-%d") == train_id:
                        # Use stations_snapshot if available, else fetch from DB
                        stations = (
                            train.get("stations_snapshot") or get_stations_from_db()
                        )
                        return {
                            "stations": stations,
                            "train": train,
                            "train_ids": [
                                (t["id"], t["departure_date"])
                                for t in data[train_number]
                            ],
                        }

    raise web.HTTPNotFound(reason="Train not found")


@routes.get("/trains/{train_number}.webmanifest")
async def webmanifest(request):
    data = {
        "name": "#" + request.match_info["train_number"],
        "short_name": "#" + request.match_info["train_number"],
        "icons": [
            {
                "src": "/static/favicon/android-chrome-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
            },
            {
                "src": "/static/favicon/android-chrome-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
            },
        ],
        "theme_color": "#ffffff",
        "background_color": "#ffffff",
        "display": "standalone",
    }
    return web.json_response(data, dumps=json_dumps)


@routes.get("/trains/{train_number}")
@routes.get("/trains/{train_number}/{train_id}")
@aiohttp_jinja2.template("train.jinja2")
async def train(request):
    data = get_trains_from_db()
    train_number = request.match_info["train_number"]
    train_id = request.match_info.get("train_id")

    if train_number in data.keys():
        if train_id is None:
            selected_train = data[train_number][0]
            # Use stations_snapshot if available, otherwise fall back to fetching stations
            stations = selected_train.get("stations_snapshot") or get_stations_from_db()
            return {
                "stations": stations,
                "train": selected_train,
                "train_ids": [
                    (t["id"], t["departure_date"]) for t in data[train_number]
                ],
            }
        else:
            for train in data[train_number]:
                try:
                    _train_id = int(train_id)
                    if train["id"] == _train_id:
                        # Use stations_snapshot if available, else fetch from DB
                        stations = (
                            train.get("stations_snapshot") or get_stations_from_db()
                        )
                        return {
                            "stations": stations,
                            "train": train,
                            "train_ids": [
                                (t["id"], t["departure_date"])
                                for t in data[train_number]
                            ],
                        }
                except ValueError:
                    if train["departure_date"].strftime("%Y-%m-%d") == train_id:
                        # Use stations_snapshot if available, else fetch from DB
                        stations = (
                            train.get("stations_snapshot") or get_stations_from_db()
                        )
                        return {
                            "stations": stations,
                            "train": train,
                            "train_ids": [
                                (t["id"], t["departure_date"])
                                for t in data[train_number]
                            ],
                        }

    raise web.HTTPNotFound(reason="Train not found")


@routes.get("/js/script.js")
async def dummy_script(request):
    return web.Response(text="")


@routes.get("/health")
async def health(request):
    """Health check endpoint that also shows data freshness"""
    try:
        metadata = get_last_update_times()
        trains = get_trains_from_db()
        stations = get_stations_from_db()

        health_data = {
            "status": "healthy",
            "trains_count": len(trains),
            "stations_count": len(stations),
            "last_train_update": metadata.get("last_train_update"),
            "last_station_update": metadata.get("last_station_update"),
        }

        # Check if data is stale (>5 minutes old)
        if metadata.get("last_train_update"):
            last_update = datetime.fromisoformat(metadata["last_train_update"])
            age_seconds = (datetime.now() - last_update).total_seconds()
            if age_seconds > 300:  # 5 minutes
                health_data["status"] = "stale"
                health_data["data_age_seconds"] = age_seconds

        return web.json_response(health_data, dumps=json_dumps)
    except Exception as e:
        return web.json_response(
            {"status": "error", "error": str(e)}, status=500, dumps=json_dumps
        )


async def request_processor(request):
    return {"request": request}


BASE_DIR = Path(__file__).resolve().parent
app = web.Application()
app.add_routes([web.static("/static", "static", append_version=True)])
app["static_root_url"] = "/static"
aiohttp_jinja2.setup(
    app,
    loader=jinja2.FileSystemLoader(BASE_DIR / "templates"),
    context_processors=[request_processor],
)
app.add_routes(routes)

if __name__ == "__main__":
    from models import get_database_url

    port = int(os.environ.get("PORT", 9000))
    print(f"Starting web app using database: {get_database_url()}")
    print(f"Starting web app on port {port}")
    print("Make sure worker.py is running to populate the database!")
    web.run_app(app, port=port)
