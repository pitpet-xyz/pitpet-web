import json
from datetime import datetime, timedelta
from urllib import request
from django.apps import apps
from django.utils.timezone import make_aware

config = apps.get_app_config("sic")


def send_request(json_data: bytes, expects_hash=True):
    headers = {"Content-Type": "application/json"}
    url = config.API_ENDPOINT
    req = request.Request(url, json_data, headers)
    with request.urlopen(req) as response:
        resp = response.read().decode("utf-8")
        print(f"got response {resp}")
        # check if response looks like a hash
        if not expects_hash:
            return resp
        is_hash = len(resp) == 64
        try:
            _v = int(resp, 16)
        except ValueError:
            is_hash = False
        if not is_hash:
            try:
                is_hash = json.loads(is_hash)
                if "error" in is_hash and "message" in is_hash:
                    message = is_hash["message"]
                    is_hash = f"Cloud blockchain replied with error: {message}"
            except:
                pass
            # reply is an error string
            raise Exception(resp)
        return resp


def upload_story(birth_hash: str, data: str):
    bindata = data.encode("utf-8")
    json_data = {
        "type": "addBlock",
        "birth_hash": birth_hash,
        "data": [byte for byte in bindata],
    }
    return send_request(json.dumps(json_data).encode("utf-8"))


def spawn_story(birth_data, data):
    bindata = data.encode("utf-8")
    binbirth_data = birth_data.encode("utf-8")
    json_data = {
        "type": "spawnBlock",
        "birth_data": [byte for byte in binbirth_data],
        "data": [byte for byte in bindata],
    }
    return send_request(json.dumps(json_data).encode("utf-8"))


def get_ttl(user):
    birth_hash = user.birth_hash
    json_data = {"type": "getTTL", "birth_hash": birth_hash}
    return send_request(json.dumps(json_data).encode("utf-8"), expects_hash=False)


def time_pass_func(job):
    last_ts = None
    if "timestamp" in job.data:
        last_ts = job.data["timestamp"]
        try:
            last_ts = datetime.fromisoformat(last_ts)
        except:
            last_ts = None
    now = make_aware(datetime.now())
    if isinstance(last_ts, datetime):
        diff = now - last_ts
        if diff < timedelta(hours=12):
            return
    req = {
        "type": "printGenesis",
    }
    genesis_hash = send_request(json.dumps(req).encode("utf-8"))
    return upload_story(genesis_hash, now.isoformat())
