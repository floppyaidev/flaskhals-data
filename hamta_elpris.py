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
    "SE1": "10Y1001A1001A44P", "SE2": "10Y1001A1001A45N", "SE3": "10Y1001A1001A46L", "SE4": "10Y1001A1001A47J",
    "NO1": "10YNO-1--------2", "NO2": "10YNO-2--------T", "NO3": "10YNO-3--------J", "NO4": "10YNO-4--------9",
    "NO5": "10Y1001A1001A48H", "DK1": "10YDK-1--------W", "DK2": "10YDK-2--------M", "FI": "10YFI-1--------U",
    "EE": "10Y1001A1001A39I", "LV": "10YLV-1001A00074", "LT": "10YLT-1001A0008Q",
    "DE": "10Y1001A1001A82H", "PL": "10YPL-AREA-----S", "NL": "10YNL----------L",
}
PSR = {
    "B01": "biobransle", "B02": "brunkol", "B03": "kolgas", "B04": "gas", "B05": "kol", "B06": "olja",
    "B07": "oljeskiffer", "B08": "torv", "B09": "geotermi", "B10": "pumpkraft", "B11": "stromkraft",
    "B12": "vattenkraft", "B13": "havsenergi", "B14": "karnkraft", "B15": "ovrig_fornybar", "B16": "sol",
    "B17": "avfall", "B18": "vind_hav", "B19": "vind_land", "B20": "ovrigt", "B25": "lagring",
}
HOURS_BACK = 6  # GitHub startar ibland körningar sent, så varje körning fyller luckor bakåt
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)
HIST = OUT / "historik"  # data/historik/SE3/2026-11.json (kvartar) och data/historik/SE3/dygn.json (dygnssnitt)


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


def spara_historik(zone, serier):
    """Lägger in nya kvartar i månadsfilerna (svensk månad räknas på UTC-tid) och räknar om dygnssnitten."""
    zdir = HIST / zone
    zdir.mkdir(parents=True, exist_ok=True)
    per_manad = {}
    for namn, punkter in serier.items():
        for ts, v in punkter.items():
            per_manad.setdefault(ts[:7], {}).setdefault(namn, {})[ts] = v
    for manad, nya in per_manad.items():
        fil = zdir / f"{manad}.json"
        old = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {"zon": zone, "manad": manad, "enhet": "MW", "serier": {}}
        for namn, punkter in nya.items():
            old["serier"].setdefault(namn, {}).update(punkter)
            old["serier"][namn] = dict(sorted(old["serier"][namn].items()))
        old["uppdaterad"] = iso(datetime.now(timezone.utc))
        tmp = fil.with_suffix(".tmp")
        tmp.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(fil)
        uppdatera_dygn(zdir, old)


def uppdatera_dygn(zdir, manadsdata):
    """Dygnssnitt per kraftslag (UTC-dygn) med antal kvartar, så att ofullständiga dygn syns."""
    fil = zdir / "dygn.json"
    dygn = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {"zon": manadsdata["zon"], "enhet": "MW (snitt)", "dygn": {}}
    summa = {}
    for namn, punkter in manadsdata["serier"].items():
        for ts, v in punkter.items():
            s = summa.setdefault(ts[:10], {}).setdefault(namn, [0.0, 0])
            s[0] += v
            s[1] += 1
    for dag, typer in summa.items():
        dygn["dygn"][dag] = {n: {"mw": round(s[0] / s[1], 1), "kvartar": s[1]} for n, s in typer.items()}
    dygn["dygn"] = dict(sorted(dygn["dygn"].items()))
    fil.write_text(json.dumps(dygn, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


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
        spara_historik(zone, data["serier"])
        antal = sum(len(s) for s in data["serier"].values())
        print(f"{zone}: sparade {antal} värden")
        time.sleep(2)


if __name__ == "__main__":
    main()
