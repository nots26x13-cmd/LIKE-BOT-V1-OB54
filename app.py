# app.py — keys.json ONLY; read-only FS safe (writes go to /tmp/keys.json)
# Stripped down to /remain and /like endpoints only.

import os
import json
import binascii
import asyncio
from threading import RLock
from urllib.parse import quote_plus
from datetime import datetime, timezone

from flask import Flask, request, jsonify, make_response
import requests
import aiohttp
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson

import like_pb2
import like_count_pb2
import uid_generator_pb2

app = Flask(__name__)

# ---------- Paths / owner ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_RO_PATH = os.path.join(BASE_DIR, "keys.json")   # packaged (read-only on Vercel)
CONFIG_RW_PATH = os.path.join("/tmp", "keys.json")     # runtime copy (writable on Vercel)
OWNER_HANDLE = "TG: @s26x_beast"

config_lock = RLock()

# ---------- Config I/O (no defaults in code, keys.json only) ----------
def _active_config_path_for_read() -> str:
    """Prefer runtime copy if it exists, else packaged file."""
    return CONFIG_RW_PATH if os.path.exists(CONFIG_RW_PATH) else CONFIG_RO_PATH

def _read_config() -> dict:
    path = _active_config_path_for_read()
    if not os.path.exists(path):
        raise FileNotFoundError("keys.json not found (no packaged file and no runtime copy).")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # minimal schema checks
    if "ALLOWED_KEYS" not in cfg or "ADMIN_KEYS" not in cfg or "RESET_TZ" not in cfg:
        raise ValueError("keys.json must include ALLOWED_KEYS, ADMIN_KEYS, RESET_TZ")
    if not isinstance(cfg["ALLOWED_KEYS"], dict) or not isinstance(cfg["ADMIN_KEYS"], list):
        raise ValueError("ALLOWED_KEYS must be object; ADMIN_KEYS must be array")
    return cfg

def get_allowed_keys() -> dict:
    with config_lock:
        return _read_config()["ALLOWED_KEYS"]

def get_admin_keys() -> set:
    with config_lock:
        return set(_read_config()["ADMIN_KEYS"])

def is_admin_key(k: str) -> bool:
    return k in get_admin_keys()

def _get_limit_for(api_key: str) -> int:
    allowed = get_allowed_keys()
    if api_key not in allowed:
        raise KeyError("key not allowed")
    return int(allowed[api_key])

# ---------- Timezone (from keys.json) ----------
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

def _today_str() -> str:
    with config_lock:
        tzname = _read_config()["RESET_TZ"]
    if ZoneInfo:
        return datetime.now(ZoneInfo(tzname)).strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ---------- Usage store (in-memory, by day) ----------
usage_store: dict[str, dict[str, int]] = {}
usage_lock = RLock()

def get_used_count(api_key: str) -> int:
    day = _today_str()
    with usage_lock:
        return usage_store.get(api_key, {}).get(day, 0)

def _set_used_count(api_key: str, value: int) -> None:
    day = _today_str()
    with usage_lock:
        per_key = usage_store.setdefault(api_key, {})
        per_key[day] = max(0, int(value))

def consume_one(api_key: str) -> int:
    day = _today_str()
    with usage_lock:
        per_key = usage_store.setdefault(api_key, {})
        per_key[day] = per_key.get(day, 0) + 1
        return per_key[day]

# ---------- Game helpers ----------
def _load_json_local(name):
    with open(os.path.join(BASE_DIR, name), "r") as f:
        return json.load(f)

def load_tokens(server_name):
    s = server_name.upper()
    if s == "IND":
        return _load_json_local("token_ind.json")
    elif s in {"BR", "US", "SAC", "NA"}:
        return _load_json_local("token_br.json")
    else:
        return _load_json_local("token_bd.json")

def encrypt_message(plaintext):
    k = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(k, AES.MODE_CBC, iv)
    padded = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded)).decode("utf-8")

def create_protobuf_message(user_id, region):
    m = like_pb2.like()
    m.uid = int(user_id)
    m.region = region
    return m.SerializeToString()

async def send_request(encrypted_uid, token, url):
    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB54"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=edata, headers=headers) as resp:
            return resp.status

async def send_multiple_requests(uid, server_name, url):
    msg = create_protobuf_message(uid, server_name)
    enc_uid = encrypt_message(msg)
    tokens = load_tokens(server_name)
    tasks = [send_request(enc_uid, tokens[i % len(tokens)]["token"], url) for i in range(300)]
    return await asyncio.gather(*tasks)

def create_protobuf(uid):
    m = uid_generator_pb2.uid_generator()
    m.krishna_ = int(uid)
    m.teamXdarks = 1
    return m.SerializeToString()

def enc(uid):
    return encrypt_message(create_protobuf(uid))

def make_request(encrypted, server_name, token):
    s = server_name.upper()
    if s == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif s in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB54"
    }
    resp = requests.post(url, data=edata, headers=headers, verify=False, timeout=30)
    binary = bytes.fromhex(resp.content.hex())
    try:
        obj = like_count_pb2.Info()
        obj.ParseFromString(binary)
        return obj
    except Exception:
        return None

def _no_store_response(payload, status=200):
    resp = make_response(jsonify(payload), status)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

def _parse_account_info(pb_obj):
    try:
        if pb_obj is None:
            return None
        js = json.loads(MessageToJson(pb_obj))
        ai = js.get("AccountInfo", {})
        uid = int(ai.get("UID", 0))
        likes = int(ai.get("Likes", 0))
        name = str(ai.get("PlayerNickname", ""))
        if uid <= 0:
            return None
        return {"uid": uid, "likes": likes, "name": name}
    except Exception:
        return None

# ---------- Endpoints ----------
@app.get("/remain")
def get_remain():
    try:
        qkey = request.args.get("key", "").strip()
        admins = get_admin_keys()
        allowed = get_allowed_keys()

        if qkey:
            if qkey in admins:
                return _no_store_response({
                    "key": qkey, "limit": "infinite", "used": 0,
                    "remaining": "infinite", "remains": "(∞/∞)", "admin": True
                })
            if qkey not in allowed:
                return _no_store_response({"error": "Invalid key for remain lookup"}, 403)
            limit = _get_limit_for(qkey)
            used = get_used_count(qkey)
            remaining = max(0, limit - used)
            return _no_store_response({
                "key": qkey, "limit": limit, "used": used,
                "remaining": remaining, "remains": f"({remaining}/{limit})"
            })

        out = []
        keys_union = set(allowed.keys()) | admins
        for k in sorted(keys_union):
            if k in admins:
                out.append({"key": k, "admin": True, "limit": "infinite",
                            "used": 0, "remaining": "infinite", "remains": "(∞/∞)"})
            else:
                limit = _get_limit_for(k)
                used = get_used_count(k)
                remaining = max(0, limit - used)
                out.append({"key": k, "admin": False, "limit": limit,
                            "used": used, "remaining": remaining,
                            "remains": f"({remaining}/{limit})"})
        return _no_store_response({"day": _today_str(), "keys": out})
    except Exception as e:
        return _no_store_response({"error": "config_error", "detail": str(e)}, 500)


@app.get("/like")
def handle_like():
    try:
        uid = request.args.get("uid")
        server_name = request.args.get("server_name", "").upper()
        api_key = request.args.get("key", "").strip()

        admins = get_admin_keys()
        allowed = get_allowed_keys()
        is_admin = api_key in admins

        if not api_key or (api_key not in allowed and not is_admin):
            return jsonify({"error": "Invalid or missing API key 🔑"}), 403
        if not uid or not server_name:
            return jsonify({"error": "UID and server_name are required"}), 400

        if not is_admin:
            limit_for_key = _get_limit_for(api_key)
            used_now = get_used_count(api_key)
            if used_now >= limit_for_key:
                return jsonify({"error": "Daily request limit reached for this key.",
                                "status": 429, "remains": f"(0/{limit_for_key})"}), 429

        tokens = load_tokens(server_name)
        token = tokens[0]["token"]
        encrypted = enc(uid)

        # BEFORE: account check
        before_obj = make_request(encrypted, server_name, token)
        before = _parse_account_info(before_obj)
        if before is None:
            remains_str = "(∞/∞)" if is_admin else f"({max(0, _get_limit_for(api_key) - get_used_count(api_key))}/{_get_limit_for(api_key)})"
            return jsonify({
                "LikesGivenByAPI": 0, "LikesafterCommand": 0, "LikesbeforeCommand": 0,
                "PlayerNickname": "Unknown",
                "UID": int(uid) if str(uid).isdigit() else uid,
                "remains": remains_str, "Owner": OWNER_HANDLE, "status": 0
            })

        # like burst
        if server_name == "IND":
            url = "https://client.ind.freefiremobile.com/LikeProfile"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/LikeProfile"
        else:
            url = "https://clientbp.ggpolarbear.com/LikeProfile"
        asyncio.run(send_multiple_requests(uid, server_name, url))

        # AFTER
        after_obj = make_request(encrypted, server_name, token)
        after = _parse_account_info(after_obj) or {"likes": before["likes"], "uid": before["uid"], "name": before["name"]}
        like_given = max(0, int(after["likes"]) - int(before["likes"]))
        status_value = 1 if like_given > 0 else 2

        if not is_admin:
            limit_for_key = _get_limit_for(api_key)
            new_used = consume_one(api_key)
            if new_used > limit_for_key:
                _set_used_count(api_key, limit_for_key)
                return jsonify({"error": "Daily request limit reached for this key.",
                                "status": 429, "remains": f"(0/{limit_for_key})"}), 429
            remaining = max(0, limit_for_key - new_used)
            remains_str = f"({remaining}/{limit_for_key})"
        else:
            remains_str = "(∞/∞)"

        return jsonify({
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": int(after["likes"]),
            "LikesbeforeCommand": int(before["likes"]),
            "PlayerNickname": str(after["name"]),
            "UID": int(after["uid"]),
            "remains": remains_str,
            "Owner": OWNER_HANDLE,
            "status": status_value
        })
    except Exception as e:
        return jsonify({"error": "config_or_runtime_error", "detail": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
