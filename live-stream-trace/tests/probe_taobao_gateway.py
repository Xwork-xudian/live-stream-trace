#!/usr/bin/env python3
"""校准：mtop 网关对已知 API 的返回，判断是“API 名错”还是“appKey 无权限”。"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DOMAINS = ["https://acs.m.taobao.com/h5"]
APIS = [
    ("mtop.taobao.media.live.room.info", "1.0", {"roomId": "123456789"}),
    ("mtop.taobao.sharepassword.query", "1.0", {"password_content": "666:/🗝AbCd1234EfGh《"}),
]


def call(base, api, ver, data, app_key="12574478"):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://tbzb.taobao.com/"})
    data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    url = f"{base}/{api}/{ver}/"
    token = ""
    for attempt in (1, 2):
        t = str(int(time.time() * 1000))
        sign = hashlib.md5(f"{token}&{t}&{app_key}&{data_str}".encode()).hexdigest() if token else ""
        p = {"jsv": "2.5.1", "appKey": app_key, "t": t, "sign": sign, "api": api, "v": ver,
             "type": "originaljson", "dataType": "json", "data": data_str}
        r = s.get(url, params=p, timeout=12)
        tk = s.cookies.get("_m_h5_tk") or ""
        if tk:
            token = tk.split("_")[0]
        try:
            out = r.json()
        except Exception:  # noqa: BLE001
            return f"HTTP {r.status_code} 非 JSON: {r.text[:120]}"
        ret = " ".join(out.get("ret") or [])
        if attempt == 1 and "TOKEN" in ret.upper():
            continue
        return f"{ret} | data={str(out.get('data'))[:120]}"
    return "(无结果)"


for base in DOMAINS:
    for api, ver, data in APIS:
        try:
            res = call(base, api, ver, data)
        except Exception as exc:  # noqa: BLE001
            res = f"异常 {type(exc).__name__}: {str(exc)[:100]}"
        print(f"{base} {api} v{ver}\n    → {res}")
