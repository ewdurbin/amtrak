import asyncio
import concurrent.futures
import json
from pathlib import Path


import aiohttp
from aiohttp import web
import aiohttp_jinja2
import jinja2

from amtrak import decrypt_data, parse_crypto, parse_trains, parse_stations

routes = web.RouteTableDef()


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
                pass
            await asyncio.sleep(10)
        except concurrent.futures.CancelledError:
            return


@routes.get("/trains/json")
async def trains_json(request):
    data = request.app["_trains"]
    return web.json_response(data)


@routes.get("/trains/{train_number}/json")
async def train_json(request):
    data = request.app["_trains"]
    train_number = request.match_info["train_number"]
    if train_number in data.keys():
        return web.json_response(data[train_number])
    return web.json_response({"message": "Train not found"}, status=404)


@routes.get("/trains/{train_number}")
@aiohttp_jinja2.template("train.jinja2")
async def train(request):
    data = request.app["_trains"]
    train_number = request.match_info["train_number"]
    if train_number in data.keys():
        return {"train": data[train_number]}
    raise web.HTTPNotFound(reason="Train not found")


@routes.get("/trains/{train_number}/_partial")
@aiohttp_jinja2.template("train_partial.jinja2")
async def train(request):
    data = request.app["_trains"]
    train_number = request.match_info["train_number"]
    if train_number in data.keys():
        return {"train": data[train_number]}
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
