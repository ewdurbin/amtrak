import asyncio
import concurrent.futures
import json
import os
import traceback
from pathlib import Path

import aiohttp
import aiohttp_jinja2
import jinja2
import orjson
import sentry_sdk
from aiohttp import web

from amtrak import decrypt_data, parse_crypto, parse_stations, parse_trains

routes = web.RouteTableDef()

if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(dsn=os.environ.get("SENTRY_DSN"))


def json_dumps(*a, **kw):
    return orjson.dumps(*a, **kw).decode()


os.environ["TZ"] = "UTC"


async def fetch_crypto():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://maps.amtrak.com/rttl/js/RoutesList.json"
        ) as resp:
            routes = await resp.json()
        async with session.get(
            "https://maps.amtrak.com/rttl/js/RoutesList.v.json"
        ) as resp:
            crypto_data = await resp.json()
    return parse_crypto(routes, crypto_data)


async def fetch_trains():
    public_key, salt, iv = await fetch_crypto()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://maps.amtrak.com/services/MapDataService/trains/getTrainsData"
        ) as resp:
            _data = await resp.read()
    return parse_trains(json.loads(decrypt_data(_data, public_key, salt, iv)))


async def fetch_stations():
    public_key, salt, iv = await fetch_crypto()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://maps.amtrak.com/services/MapDataService/stations/trainStations"
        ) as resp:
            _data = await resp.read()
    return parse_stations(json.loads(decrypt_data(_data, public_key, salt, iv)))


async def refresh_trains_task(app):
    while True:
        try:
            print("refreshing trains...")
            try:
                _trains = await fetch_trains()
                app["_trains"] = _trains
            except concurrent.futures.CancelledError:
                raise
            except Exception as exc:
                traceback.print_exception(exc)
            await asyncio.sleep(10)
        except concurrent.futures.CancelledError:
            return


async def refresh_stations_task(app):
    while True:
        try:
            print("refreshing stations...")
            try:
                _stations = await fetch_stations()
                app["_stations"] = _stations
            except concurrent.futures.CancelledError:
                raise
            except Exception as exc:
                traceback.print_exception(exc)
            await asyncio.sleep(120)
        except concurrent.futures.CancelledError:
            return


@routes.get("/")
@aiohttp_jinja2.template("index.jinja2")
async def index(request):
    return {}


@routes.get("/trains")
@aiohttp_jinja2.template("trains.jinja2")
async def trains(request):
    data = request.app["_trains"]
    return {"trains": data}


@routes.get("/trains/json")
async def trains_json(request):
    data = request.app["_trains"]
    return web.json_response(data, dumps=json_dumps)


@routes.get("/trains/{train_number}/json")
@routes.get("/trains/{train_number}/{train_id}/json")
async def train_json(request):
    data = request.app["_trains"]
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
    data = request.app["_trains"]
    train_number = request.match_info["train_number"]
    train_id = request.match_info.get("train_id")
    if train_number in data.keys():
        if train_id is None:
            return {
                "train": data[train_number][0],
                "train_ids": [t["id"] for t in data[train_number]],
            }
        else:
            for train in data[train_number]:
                try:
                    _train_id = int(train_id)
                    if train["id"] == _train_id:
                        return {
                            "stations": request.app["_stations"],
                            "train": train,
                            "train_ids": [
                                (t["id"], t["departure_date"])
                                for t in data[train_number]
                            ],
                        }
                except ValueError:
                    if train["departure_date"].strftime("%Y-%m-%d") == train_id:
                        return {
                            "stations": request.app["_stations"],
                            "train": train,
                            "train_ids": [
                                (t["id"], t["departure_date"])
                                for t in data[train_number]
                            ],
                        }
    raise web.HTTPNotFound(reason="Train not found")


@routes.get("/trains/{train_number}")
@routes.get("/trains/{train_number}/{train_id}")
@aiohttp_jinja2.template("train.jinja2")
async def train(request):
    data = request.app["_trains"]
    train_number = request.match_info["train_number"]
    train_id = request.match_info.get("train_id")
    if train_number in data.keys():
        if train_id is None:
            return {
                "stations": request.app["_stations"],
                "train": data[train_number][0],
                "train_ids": [
                    (t["id"], t["departure_date"]) for t in data[train_number]
                ],
            }
        else:
            for train in data[train_number]:
                try:
                    _train_id = int(train_id)
                    if train["id"] == _train_id:
                        return {
                            "stations": request.app["_stations"],
                            "train": train,
                            "train_ids": [
                                (t["id"], t["departure_date"])
                                for t in data[train_number]
                            ],
                        }
                except ValueError:
                    if train["departure_date"].strftime("%Y-%m-%d") == train_id:
                        return {
                            "stations": request.app["_stations"],
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


async def cancel_tasks(app):
    for task in app["tasks"]:
        await task.cancel()


async def start_task(app):
    app["_trains"] = {}
    app["_stations"] = {}
    _refresh_trains_task = asyncio.ensure_future(
        refresh_trains_task(app),
        loop=asyncio.get_event_loop(),
    )
    app["tasks"].append(_refresh_trains_task)
    _refresh_stations_task = asyncio.ensure_future(
        refresh_stations_task(app),
        loop=asyncio.get_event_loop(),
    )
    app["tasks"].append(_refresh_stations_task)


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
app["tasks"] = []
app.on_startup.append(start_task)
app.on_shutdown.append(cancel_tasks)
app.add_routes(routes)

if __name__ == "__main__":
    web.run_app(app, port=9000)
