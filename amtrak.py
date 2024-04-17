import datetime
import base64
import json
from collections import defaultdict, OrderedDict
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.padding import PKCS7

# This is a port of a JS library for decrypting amtrak api data
# Source: https://github.com/mgwalker/amtrak-api/blob/main/src/data/crypto.js


def parse_crypto(routes, crypto_data):
    # Our Cryptographic indicies are based on the sum of the "ZoomLevel" from routes
    _index = sum([route.get("ZoomLevel", 0) for route in routes])

    public_key = crypto_data["arr"][_index]
    # Salt and Initialization Vectors are based on the index found based on
    # the length of any given Salt or Initialization Vector
    salt = bytes.fromhex(crypto_data["s"][len(crypto_data["s"][0])])
    iv = bytes.fromhex(crypto_data["v"][len(crypto_data["v"][0])])

    return (public_key, salt, iv)


def fetch_crypto():
    routes = requests.get("https://maps.amtrak.com/rttl/js/RoutesList.json").json()
    # The actual public keys, salt, and initialization vectors are served from another file
    crypto_data = requests.get(
        "https://maps.amtrak.com/rttl/js/RoutesList.v.json"
    ).json()
    return parse_crypto(routes, crypto_data)


# Define our decryption function
def decrypt(data, salt, iv, key_derivation_password):
    _data = base64.b64decode(data)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=salt,
        iterations=1000,
    )
    key = kdf.derive(key_derivation_password.encode())

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(_data) + decryptor.finalize()


# Define an approach for decrypting our data payloads overall
def decrypt_data(_data, public_key, salt, iv):
    MASTER_SEGMENT = (
        88  # The last 88 bytes of the payload are the encrypted private key
    )
    ciphertext = _data[:-MASTER_SEGMENT]
    private_key_cipher = _data[-MASTER_SEGMENT:]
    private_key = (
        decrypt(private_key_cipher, salt, iv, public_key).decode().split("|")[0]
    )
    padded_data = decrypt(ciphertext, salt, iv, private_key)

    unpadder = PKCS7(128).unpadder()
    data = unpadder.update(padded_data) + unpadder.finalize()

    return data


def parse_stations(stations):
    _stations = {}
    for _station in stations["StationsDataResponse"]["features"]:
        _stations[_station["properties"]["Code"]] = {
            "geometry": _station["geometry"],
            "station_name": _station["properties"]["StationName"],
        }
    return _stations


TIMEZONES = {
    "E": ZoneInfo("America/New_York"),
    "America/New_York": ZoneInfo("America/New_York"),
    "C": ZoneInfo("America/Chicago"),
    "America/Chicago": ZoneInfo("America/Chicago"),
    "M": ZoneInfo("America/Denver"),
    "America/Denver": ZoneInfo("America/Denver"),
    "P": ZoneInfo("America/Los_Angeles"),
    "America/Los_Angeles": ZoneInfo("America/Los_Angeles"),
}


def parse_date(date, timezone_identifier):
    if date is not None:
        try:
            return datetime.datetime.strptime(date, "%m/%d/%Y %H:%M:%S").astimezone(
                tz=TIMEZONES[timezone_identifier]
            )
        except ValueError:
            return datetime.datetime.strptime(date, "%m/%d/%Y %H:%M:%S %p").astimezone(
                tz=TIMEZONES[timezone_identifier]
            )
    return None


def parse_trains(trains):
    _trains = defaultdict(list)
    for _train in trains["features"]:
        _stations = OrderedDict()
        for i in range(100):
            data = _train["properties"].get(f"Station{i}", None)
            if data is not None:
                data = json.loads(data)
                _stations[data["code"]] = {
                    "code": data["code"],
                    "tz": TIMEZONES[data["tz"]].key,
                    "arrived": True if "postarr" in data.keys() else False,
                    "departed": True if "postdep" in data.keys() else False,
                    "scheduled": {
                        "arrival": parse_date(data.get("scharr", None), data.get("tz")),
                        "departure": parse_date(
                            data.get("schdep", None), data.get("tz")
                        ),
                        "comment": data.get("schcmnt", None),
                    },
                    "estimated": {
                        "arrival": parse_date(data.get("estarr", None), data.get("tz")),
                        "departure": parse_date(
                            data.get("estdep", None), data.get("tz")
                        ),
                        "arrival_comment": data.get("estarrcmnt", None),
                        "departure_comment": data.get("estdepcmnt", None),
                    },
                    "actual": {
                        "arrival": parse_date(
                            data.get("postarr", None), data.get("tz")
                        ),
                        "departure": parse_date(
                            data.get("postdep", None), data.get("tz")
                        ),
                        "comment": data.get("postcmnt", None),
                    },
                }
        cur_tz = (
            _stations[_train["properties"]["EventCode"]]["tz"]
            if _train["properties"]["EventCode"] is not None
            else _train["properties"]["OriginTZ"]
        )
        _trains[_train["properties"]["TrainNum"]].append(
            {
                "route_name": _train["properties"]["RouteName"],
                "train_number": _train["properties"]["TrainNum"],
                "id": _train["properties"]["ID"],
                "last_update": parse_date(_train["properties"]["LastValTS"], cur_tz),
                "stations": _stations,
                "last_fetched": datetime.datetime.now()
                .replace(microsecond=0)
                .astimezone(tz=TIMEZONES[cur_tz]),
            }
        )
    return _trains


if __name__ == "__main__":
    _data = requests.get("https://maps.amtrak.com/rttl/js/RoutesList.json").content
    with open("routes.json", "w") as f:
        f.write(_data.decode())

    public_key, salt, iv = fetch_crypto()

    _data = requests.get(
        "https://maps.amtrak.com/services/MapDataService/trains/getTrainsData"
    ).content
    data = decrypt_data(_data, public_key, salt, iv)
    with open("trains.json", "w") as f:
        f.write(data.decode())

    _data = requests.get(
        "https://maps.amtrak.com/services/MapDataService/stations/trainStations"
    ).content
    data = decrypt_data(_data, public_key, salt, iv)
    with open("stations.json", "w") as f:
        f.write(data.decode())
