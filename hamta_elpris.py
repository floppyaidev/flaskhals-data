import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN = os.environ.get("ENTSOE_TOKEN")
if not TOKEN:
    sys.exit("Sätt miljövariabeln ENTSOE_TOKEN först.")

API = "https://web-api.tp.entsoe.eu/api"
ZONES = {
    "SE1": "10Y1001A1001A44P",
    "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L",
    "SE4": "10Y1001A1001A47J",
}
PSR = {
    "B04": "gas", "B12": "vattenkraft", "B14": "karnkraft", "B16": "sol",
    "B18": "vind_hav", "B19": "vind_land", "B20": "ovrigt",
}
HOURS_BACK = 3
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)


class RateLimit(Exception):
    pass


def localname(el):
    return el.tag.split("}")[-1]


def find_text(el, name):
    for c in el.iter():
        if localname(c) == name and c.text:
            return c.text.strip()
    return None


def fmt(dt):
    return dt.strftime("%Y%m%d%H%M")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def fetch(code, start, end):
    params = {
        "securityToken": TOKEN,
        "documentType": "A75",
        "processType": "A16",
        "in_Domain": code,
        "periodStart": fmt(start),
        "periodEnd": fmt(end),
    }
    url = API + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimit()
        return e.read()


def parse(body):
    root = ET.fromstring(body)
    if localname(root) == "Acknowledgement_MarketDocument":
        texts = [e.text for e in root.iter() if localname(e) == "text"]
        return {"ack": texts}

    series_out = {}
    for ts in root.iter():
        if localname(ts) != "TimeSeries":
            continue
        psr = find_text(ts, "psrType")
        curve = find_text(ts, "curveType")
        per = next(e for e in ts.iter() if localname(e) == "Period")
        start = datetime.strptime(find_text(per, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        end = datetime.strptime(find_text(per, "end"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        step = timedelta(minutes=15) if find_text(per, "resolution") == "PT15M" else timedelta(hours=1)
        npts = int((end - start) / step)

        vals = {}
        for p in per.iter():
            if localname(p) == "Point":
                vals[int(find_text(p, "position"))] = float(find_text(p, "quantity"))

        name = PSR.get(psr, psr)
        series = series_out.setdefault(name, {})
        last = None
        for pos in range(1, npts + 1):
            if pos in vals:
                last = vals[pos]
            elif curve != "A03":
                continue  # A01: saknade punkter är verkligen saknade
            # A03: värdet gäller tills nästa ändring
            if last is None:
                continue
            series[iso(start + (pos - 1) * step)] = last
    return {"serier": series_out}


def main():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now = now - timedelta(minutes=now.minute % 15)
    start = now - timedelta(hours=HOURS_BACK)
    end = now

    for zone, code in ZONES.items():
        try:
            body = fetch(code, start, end)
        except RateLimit:
            print("429: för många anrop, avbryter. Försök igen om ca 10 minuter.")
            break
        except Exception as e:
            print(f"{zone}: nätverksfel: {e}")
            time.sleep(2)
            continue

        try:
            data = parse(body)
        except ET.ParseError:
            print(f"{zone}: svaret är inte XML: {body[:300]!r}")
            time.sleep(2)
            continue

        if "ack" in data:
            print(f"{zone}: inget data ännu {data['ack']}")
            time.sleep(2)
            continue

        payload = {
            "zon": zone,
            "uppdaterad": iso(datetime.now(timezone.utc)),
            "fönster": [iso(start), iso(end)],
            "serier": data["serier"],
        }
        tmp = OUT / f"{zone}.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(OUT / f"{zone}.json")
        antal = sum(len(s) for s in data["serier"].values())
        print(f"{zone}: sparade {antal} värden")
        time.sleep(2)


if __name__ == "__main__":
    main()
