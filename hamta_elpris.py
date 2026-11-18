import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
import io
import zipfile
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


def fetch(code, start, end, doc="A75", proc="A16"):
    params = {
        "securityToken": TOKEN,
        "documentType": doc,
        "processType": proc,
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


def hamta_serier(code, start, end, doc, proc):
    """Som fetch+parse men tyst: returnerar {} om det inte finns data."""
    try:
        data = parse(fetch(code, start, end, doc, proc))
    except RateLimit:
        raise
    except Exception:
        return {}
    return data.get("serier", {})


def uppdatera_kapacitet():
    """Installerad effekt per kraftslag (A68), högst en gång per dygn, till data/kapacitet.json."""
    fil = OUT / "kapacitet.json"
    if fil.exists():
        old = json.loads(fil.read_text(encoding="utf-8"))
        if old.get("uppdaterad", "")[:10] == iso(datetime.now(timezone.utc))[:10]:
            return
    y = datetime.now(timezone.utc).year
    start = datetime(y, 1, 1, tzinfo=timezone.utc)
    out = {"uppdaterad": iso(datetime.now(timezone.utc)), "enhet": "MW", "zoner": {}}
    for zone, code in ZONES.items():
        serier = hamta_serier(code, start, datetime(y + 1, 1, 1, tzinfo=timezone.utc), "A68", "A33")
        out["zoner"][zone] = {n: list(v.values())[-1] for n, v in serier.items() if v}
        time.sleep(1)
    fil.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("kapacitet uppdaterad")


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


NUC_ZONES = ["SE3", "FI", "NL"]  # zoner med kärnkraft bland de som hämtas


def parse_enheter(body):
    """Produktion eller kapacitet per produktionsenhet (A73/A71): [{namn, mrid, psr, punkter{ts: mw}}]."""
    root = ET.fromstring(body)
    if localname(root) == "Acknowledgement_MarketDocument":
        return []
    out = []
    for ts in root.iter():
        if localname(ts) != "TimeSeries":
            continue
        psr = find_text(ts, "psrType")
        res = next((e for e in ts.iter() if localname(e) == "PowerSystemResources"), None)
        namn = find_text(res, "name") if res is not None else None
        mrid = find_text(res, "mRID") if res is not None else None
        pts = {}
        for per in (e for e in ts.iter() if localname(e) == "Period"):
            start = datetime.strptime(find_text(per, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            r = find_text(per, "resolution")
            step = timedelta(minutes=15) if r == "PT15M" else timedelta(minutes=30) if r == "PT30M" else timedelta(hours=1)
            for p in per.iter():
                if localname(p) == "Point":
                    pts[iso(start + (int(find_text(p, "position")) - 1) * step)] = float(find_text(p, "quantity"))
        out.append({"namn": namn or mrid, "mrid": mrid, "psr": psr, "punkter": pts})
    return out


def parse_avbrott(body, stats):
    """Avbrott (A80) kommer som zip med en XML per meddelande. Returnerar senaste versionen per meddelande."""
    if not body.startswith(b"PK"):
        stats["inte_zip"] = body[:200].decode("utf-8", "replace")
        return []
    senaste = {}
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        stats["filer"] = len(z.namelist())
        for fn in z.namelist():
            root = ET.fromstring(z.read(fn))
            mrid, rev = find_text(root, "mRID"), int(find_text(root, "revisionNumber") or 0)
            status = find_text(root, "docStatus")
            for ts in (e for e in root.iter() if localname(e) == "TimeSeries"):
                psr = None
                namn = None
                for e in ts.iter():
                    ln = localname(e)
                    if ln.endswith("psrType") and e.text:
                        psr = e.text.strip()
                    if ln.endswith("RegisteredResource.name") and e.text and not namn:
                        namn = e.text.strip()
                    if ln.endswith("PowerSystemResources.name") and e.text:
                        namn = e.text.strip()  # själva reaktorn går före anläggningen
                stats["typer"][psr] = stats["typer"].get(psr, 0) + 1
                if psr != "B14":
                    continue
                tillg = [float(find_text(p, "quantity")) for p in ts.iter() if localname(p) == "Point"]
                orsak = [e.text.strip() for e in root.iter() if localname(e) == "text" and e.text]
                post = {
                    "namn": namn,
                    "typ": "planerat" if find_text(ts, "businessType") == "A53" else "oplanerat",
                    "start": find_text(ts, "start_DateAndOrTime.date") + "T" + (find_text(ts, "start_DateAndOrTime.time") or "00:00:00Z")[:5] + "Z",
                    "slut": find_text(ts, "end_DateAndOrTime.date") + "T" + (find_text(ts, "end_DateAndOrTime.time") or "00:00:00Z")[:5] + "Z",
                    "nominell_mw": float(find_text(ts, "nominalP") or 0) or None,
                    "tillganglig_mw": min(tillg) if tillg else None,
                    "orsak": "; ".join(dict.fromkeys(orsak))[:300] or None,
                    "status": status,
                }
                if mrid not in senaste or rev >= senaste[mrid][0]:
                    senaste[mrid] = (rev, post)
    return [p for r, p in senaste.values() if p["status"] != "A09"]  # A09 = återkallat


def uppdatera_karnkraft(now):
    """Varje reaktors senaste produktion (A73), dess installerade effekt (A71, en gång per dygn) och
    aktuella och kommande avbrott (A80) till data/karnkraft.json."""
    fil = OUT / "karnkraft.json"
    old = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {}
    kap = old.get("kapacitet", {})
    if old.get("kapacitet_dag") != iso(now)[:10]:
        kap = {}
        y = now.year
        for zone in NUC_ZONES:
            for u in parse_enheter(fetch(ZONES[zone], datetime(y, 1, 1, tzinfo=timezone.utc), datetime(y + 1, 1, 1, tzinfo=timezone.utc), "A71", "A33")):
                if u["psr"] == "B14" and u["punkter"]:
                    kap[u["namn"]] = list(u["punkter"].values())[-1]
            time.sleep(1)
    enheter = []
    for zone in NUC_ZONES:
        body = fetch(ZONES[zone], now - timedelta(hours=HOURS_BACK), now, "A73", "A16")
        alla = parse_enheter(body)
        print(f"A73 {zone}: {len(alla)} enheter, typer {sorted(set(u['psr'] or '-' for u in alla))}" + ("" if alla else f", svar: {body[:250]!r}"))
        for u in alla:
            if u["psr"] != "B14" or not u["punkter"]:
                continue
            ts = sorted(u["punkter"])[-1]
            enheter.append({"zon": zone, "namn": u["namn"], "mw": u["punkter"][ts], "tid": ts, "kapacitet_mw": kap.get(u["namn"])})
        time.sleep(1)
    avbrott = []
    for zone in NUC_ZONES:
        body = fetch_raw({"documentType": "A80", "BiddingZone_Domain": ZONES[zone],
                          "periodStart": fmt(now - timedelta(days=1)), "periodEnd": fmt(now + timedelta(days=60))})
        stats = {"typer": {}}
        res = parse_avbrott(body, stats)
        print(f"A80 {zone}: {stats.get('filer', 0)} meddelanden, typer {stats['typer']}, kärnkraft {len(res)}" + (f", svar: {stats['inte_zip']!r}" if "inte_zip" in stats else ""))
        for a in res:
            a["zon"] = zone
            avbrott.append(a)
        time.sleep(1)
    avbrott.sort(key=lambda a: (a["zon"], a["namn"] or "", a["start"]))
    out = {"uppdaterad": iso(datetime.now(timezone.utc)), "enhet": "MW", "kapacitet_dag": iso(now)[:10] if kap else None,
           "kapacitet": kap, "enheter": sorted(enheter, key=lambda e: (e["zon"], e["namn"])), "avbrott": avbrott}
    fil.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"kärnkraft: {len(enheter)} reaktorer, {len(avbrott)} avbrott")


def fetch_raw(params):
    url = API + "?" + urllib.parse.urlencode(dict(params, securityToken=TOKEN))
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimit()
        return e.read()


def main():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now = now - timedelta(minutes=now.minute % 15)
    start = now - timedelta(hours=HOURS_BACK)
    end = now

    try:
        uppdatera_kapacitet()
    except RateLimit:
        print("429 vid kapacitet, försöker nästa körning")
    try:
        uppdatera_karnkraft(now)
    except RateLimit:
        print("429 vid kärnkraft, försöker nästa körning")
    except Exception as e:
        print(f"kärnkraft: fel {e!r}")
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

        prognos = hamta_serier(code, start, end + timedelta(hours=1), "A69", "A01")  # vind- och solprognos
        time.sleep(1)
        payload = {
            "zon": zone,
            "uppdaterad": iso(datetime.now(timezone.utc)),
            "fönster": [iso(start), iso(end)],
            "serier": data["serier"],
            "prognos": prognos,
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
