import asyncio
import concurrent.futures
import json
import traceback
from functools import partial
from pathlib import Path


import aiohttp
from aiohttp import web
import aiohttp_jinja2
import jinja2

from amtrak import decrypt_data, parse_crypto, parse_trains
from util import DateTimeEncoder

routes = web.RouteTableDef()

json_dumps = partial(json.dumps, cls=DateTimeEncoder)

import os

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
async def train_json(request):
    data = request.app["_trains"]
    train_number = request.match_info["train_number"]
    if train_number in data.keys():
        return web.json_response(data[train_number], dumps=json_dumps)
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
                if train["id"] == int(train_id):
                    return {
                        "train": train,
                        "train_ids": [t["id"] for t in data[train_number]],
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
                "train": data[train_number][0],
                "train_ids": [t["id"] for t in data[train_number]],
            }
        else:
            for train in data[train_number]:
                if train["id"] == int(train_id):
                    return {
                        "train": train,
                        "train_ids": [t["id"] for t in data[train_number]],
                    }
    raise web.HTTPNotFound(reason="Train not found")


async def cancel_tasks(app):
    for task in app["tasks"]:
        await task.cancel()


async def start_task(app):
    app["_trains"] = {}
    _refresh_trains_task = asyncio.ensure_future(
        refresh_trains_task(app),
        loop=asyncio.get_event_loop(),
    )
    app["tasks"].append(_refresh_trains_task)


BASE_DIR = Path(__file__).resolve().parent
app = web.Application()
aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(BASE_DIR / "templates"))
app["tasks"] = []
app.on_startup.append(start_task)
app.on_shutdown.append(cancel_tasks)
app.add_routes(routes)

if __name__ == "__main__":
    web.run_app(app, port=9000)
