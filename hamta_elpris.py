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


def parse_avbrott(body, stats, psr_keep=("B14",)):
    """Avbrott (A77/A80) kommer som zip med en XML per meddelande. Returnerar senaste versionen per meddelande.
    psr_keep: kraftslag att behålla (None = alla)."""
    if not body.startswith(b"PK"):
        stats["inte_zip"] = body[:200].decode("utf-8", "replace")
        return []
    senaste = {}
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        stats["filer"] = len(z.namelist())
        for fn in z.namelist():
            root = ET.fromstring(z.read(fn))
            mrid, rev = find_text(root, "mRID"), int(find_text(root, "revisionNumber") or 0)
            status = next((find_text(e, "value") or (e.text or "").strip() for e in root.iter() if localname(e) == "docStatus"), None)
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
                if psr_keep is not None and psr not in psr_keep:
                    continue
                tillg = [float(find_text(p, "quantity")) for p in ts.iter() if localname(p) == "Point"]
                orsak = [e.text.strip() for e in root.iter() if localname(e) == "text" and e.text]
                post = {
                    "namn": namn,
                    "typ": "planerat" if find_text(ts, "businessType") == "A53" else "oplanerat",
                    "start": find_text(ts, "start_DateAndOrTime.date") + "T" + (find_text(ts, "start_DateAndOrTime.time") or "00:00:00Z")[:5] + "Z",
                    "slut": find_text(ts, "end_DateAndOrTime.date") + "T" + (find_text(ts, "end_DateAndOrTime.time") or "00:00:00Z")[:5] + "Z",
                    "nominell_mw": next((float(e.text) for e in ts.iter() if localname(e).endswith("nominalP") and e.text), None),
                    "tillganglig_mw": min(tillg) if tillg else None,
                    "orsak": "; ".join(dict.fromkeys(orsak))[:300] or None,
                    "status": status,
                    "kraftslag": PSR.get(psr, "ovrigt"),
                }
                if mrid not in senaste or rev >= senaste[mrid][0]:
                    senaste[mrid] = (rev, post)
    return [p for r, p in senaste.values() if p["status"] != "A09"]  # A09 = återkallat


# Reaktorerna i zonerna (namn som i ENTSO-E:s avbrottsmeddelanden, ungefärlig nettoeffekt i MW).
# Produktion per reaktor (A73) publiceras först dagar i efterhand, så statusen bygger på avbrotten.
REAKTORER = {
    "SE3": [("Forsmark block 1", 984), ("Forsmark block 2", 1120), ("Forsmark block 3", 1167),
            ("Oskarshamn 3", 1400), ("Ringhals 3", 1074), ("Ringhals 4", 1130)],
    "FI": [("Loviisa 1", 507), ("Loviisa 2", 507), ("Olkiluoto 1", 890), ("Olkiluoto 2", 890), ("Olkiluoto 3", 1600)],
    "NL": [("Borssele", 485)],
}


def uppdatera_karnkraft(now):
    """Aktuella och kommande avbrott (A77 produktionsenheter, A80 generatorenheter) till data/karnkraft.json."""
    fil = OUT / "karnkraft.json"
    old = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {}
    if old.get("uppdaterad") and datetime.now(timezone.utc) - datetime.strptime(old["uppdaterad"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) < timedelta(minutes=55):
        print("kärnkraft: uppdaterad för mindre än en timme sedan, hoppar över")
        return
    avbrott = []
    for zone in NUC_ZONES:
        for doc in ("A77", "A80"):
            body = fetch_raw({"documentType": doc, "BiddingZone_Domain": ZONES[zone],
                              "periodStart": fmt(now - timedelta(days=1)), "periodEnd": fmt(now + timedelta(days=60))})
            stats = {"typer": {}}
            res = parse_avbrott(body, stats)
            print(f"{doc} {zone}: {stats.get('filer', 0)} meddelanden, kärnkraft {len(res)}" + (f", svar: {ack_text(body)}" if "inte_zip" in stats else ""))
            for a in res:
                a["zon"] = zone
                if not any(x["namn"] == a["namn"] and x["start"] == a["start"] for x in avbrott):
                    avbrott.append(a)
            time.sleep(1)
    avbrott.sort(key=lambda a: (a["zon"], a["namn"] or "", a["start"]))
    reaktorer = [{"zon": z, "namn": n, "mw": mw} for z, lista in REAKTORER.items() for n, mw in lista]
    out = {"uppdaterad": iso(datetime.now(timezone.utc)), "enhet": "MW", "reaktorer": reaktorer, "avbrott": avbrott}
    fil.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"kärnkraft: {len(avbrott)} avbrott")


def uppdatera_avbrott(now, budget_s=150):
    """Pågående avbrott för alla zoner och kraftslag (A77 + A80) till data/avbrott.json.
    Kraftverk utan avbrott syns inte här. Körs högst en gång i timmen och har en tidsgräns."""
    fil = OUT / "avbrott.json"
    old = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {}
    if old.get("uppdaterad") and datetime.now(timezone.utc) - datetime.strptime(old["uppdaterad"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) < timedelta(minutes=55):
        print("avbrott: uppdaterade för mindre än en timme sedan, hoppar över")
        return
    t0 = time.time()
    zoner = {}
    klara = 0
    for zone, code in ZONES.items():
        if time.time() - t0 > budget_s:
            print(f"avbrott: tidsgränsen nådd efter {klara} zoner, behåller gamla värden för resten")
            break
        lista = []
        ok = True
        for doc in ("A77", "A80"):
            try:
                body = fetch_raw({"documentType": doc, "BiddingZone_Domain": code,
                                  "periodStart": fmt(now - timedelta(hours=1)), "periodEnd": fmt(now + timedelta(hours=24))})
                stats = {"typer": {}}
                res = parse_avbrott(body, stats, psr_keep=None)
            except RateLimit:
                raise
            except Exception as e:
                print(f"avbrott {doc} {zone}: fel {e!r}")
                ok = False
                continue
            for a in res:
                a.pop("status", None)
                if a.get("orsak"):
                    a["orsak"] = a["orsak"][:160]
                if not any(x["namn"] == a["namn"] and x["start"] == a["start"] for x in lista):
                    lista.append(a)
            time.sleep(0.5)
        # bara avbrott som pågår nu eller börjar inom ett dygn, störst bortfall först
        lim = iso(now + timedelta(hours=24))
        lista = [a for a in lista if a["slut"] > iso(now) and a["start"] <= lim]
        lista.sort(key=lambda a: -((a.get("nominell_mw") or 0) - (a.get("tillganglig_mw") or 0)))
        if ok or lista:
            zoner[zone] = lista[:40]
            klara += 1
        elif zone in old.get("zoner", {}):
            zoner[zone] = old["zoner"][zone]
    for zone in ZONES:
        if zone not in zoner and zone in old.get("zoner", {}):
            zoner[zone] = old["zoner"][zone]
    out = {"uppdaterad": iso(datetime.now(timezone.utc)), "enhet": "MW", "zoner": zoner}
    fil.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"avbrott: {sum(len(v) for v in zoner.values())} avbrott i {len(zoner)} zoner")


def parse_enheter(body):
    """A73 (produktion per kraftverk/enhet). Returnerar {namn: {kraftslag, mw, t}} med senaste punkten per enhet."""
    root = ET.fromstring(body)
    if localname(root) == "Acknowledgement_MarketDocument":
        return {}
    out = {}
    for ts in (e for e in root.iter() if localname(e) == "TimeSeries"):
        psr = find_text(ts, "psrType")
        namn = None
        for e in ts.iter():
            if localname(e) == "PowerSystemResources":
                namn = find_text(e, "name") or find_text(e, "mRID")
                break
        if not namn:
            continue
        for per in (e for e in ts.iter() if localname(e) == "Period"):
            start = datetime.strptime(find_text(per, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            res = find_text(per, "resolution")
            step = timedelta(minutes=15) if res == "PT15M" else timedelta(minutes=30) if res == "PT30M" else timedelta(hours=1)
            pts = [(int(find_text(p, "position")), float(find_text(p, "quantity"))) for p in per.iter() if localname(p) == "Point"]
            if not pts:
                continue
            pos, q = max(pts)
            t = iso(start + (pos - 1) * step)
            if namn not in out or t > out[namn]["t"]:
                out[namn] = {"namn": namn, "kraftslag": PSR.get(psr, "ovrigt"), "mw": round(max(0.0, q)), "t": t}
    return out


# A73 (produktion per enhet) frågas per kontrollområde, inte per elområde.
CONTROL = {"SE": "10YSE-1--------K", "NO": "10YNO-0--------C", "DK": "10Y1001A1001A796", "FI": "10YFI-1--------U",
           "EE": "10Y1001A1001A39I", "LV": "10YLV-1001A00074", "LT": "10YLT-1001A0008Q", "PL": "10YPL-AREA-----S",
           "NL": "10YNL----------L"}  # DE har fyra kontrollområden och många enheter, hoppas över


def land(zone):
    return zone[:2] if zone[:2] in ("SE", "NO", "DK") else zone


def norm(x):
    x = (x or "").lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("ø", "o"), ("æ", "a")):
        x = x.replace(a, b)
    return "".join(ch for ch in x if ch.isalnum())


def samma(a, b):
    a, b = norm(a), norm(b)
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


# Stora svenska vattenkraftverk och vilken älv de ligger i (namnet räcker för att känna igen dem i ENTSO-E:s data).
ALVAR = {
    "Luleälven": ["Harsprånget", "Porjus", "Messaure", "Letsi", "Ligga", "Vietas", "Seitevare", "Porsi", "Laxede", "Boden",
                  "Parki", "Akkats", "Ritsem", "Edefors", "Vittjärv", "Randi", "Abelvattnet"],
    "Skellefteälven": ["Bastusel", "Kvistforsen", "Rebnis", "Sädva", "Rengård", "Båtfors", "Finnfors", "Granfors", "Krångfors",
                       "Selsfors", "Slagnäs", "Grytfors", "Gallejaur", "Vargfors"],
    "Umeälven": ["Stornorrfors", "Harrsele", "Pengfors", "Juktan", "Gejmån", "Bålforsen", "Betsele", "Rusfors", "Tuggen",
                 "Grundfors", "Ajaure", "Gardiken", "Umluspen", "Stensele", "Hällforsen"],
    "Ångermanälven": ["Kilforsen", "Stalon", "Hällby", "Forsmo", "Nämforsen", "Moforsen", "Storfinnforsen", "Ramsele",
                      "Långbjörn", "Sollefteå", "Edensforsen", "Bågede", "Gulsele", "Junsterforsen", "Hjälta", "Kultsjön",
                      "Lövön", "Tåsan", "Korsselbränna", "Stenkullafors", "Volgsjöfors", "Bodum", "Fjällsjö", "Hoting",
                      "Gäddede", "Linnvasselv", "Stalon"],
    "Indalsälven": ["Bergeforsen", "Krångede", "Stadsforsen", "Hammarforsen", "Järkvissle", "Midskog", "Gammelänge",
                    "Hölleforsen", "Ragunda", "Bispgården", "Kattstrupeforsen", "Näverede", "Stugun", "Svarthålsforsen",
                    "Järpströmmen", "Olden", "Tännforsen", "Granboforsen", "Sillre", "Lit", "Storfinnforsen"],
    "Ljungan": ["Järnvägsforsen", "Nederede", "Viforsen", "Matfors", "Ljunga", "Parteboda", "Torpshammar", "Flaten",
                "Bursnäs", "Gidböle", "Rätan", "Hissmofors"],
    "Ljusnan": ["Laforsen", "Ljusne", "Lassbyn", "Sveg", "Byarforsen", "Krokströmmen", "Storåforsen", "Halvfari", "Höljebro",
                "Edeforsen", "Kolsjön", "Landafors", "Ljusnefors"],
    "Dalälven": ["Trängslet", "Lanforsen", "Älvkarleby", "Mockfjärd", "Untra", "Kvarnsveden", "Avesta", "Spjutmo",
                 "Lima", "Blyberg", "Väsa", "Forshuvud", "Domnarvet"],
    "Göta älv": ["Olidan", "Hojum", "Trollhättan", "Lilla Edet", "Vargön"],
    # Finland
    "Kemijoki": ["Petäjäskoski", "Pirttikoski", "Valajaskoski", "Isohaara", "Ossauskoski", "Taivalkoski", "Vanttauskoski",
                 "Seitakorva", "Kelukoski", "Rytikoski", "Kokkosniva", "Permantokoski", "Juotas", "Matarakoski", "Vajukoski",
                 "Kurittaankoski", "Kemijoki"],
    "Oulujoki": ["Pyhäkoski", "Montta", "Merikoski", "Nuojua", "Utanen", "Jylhämä", "Pälli", "Leppiniemi", "Oulujoki"],
    "Iijoki": ["Raasakka", "Maalismaa", "Kierikki", "Pahkakoski", "Haapakoski", "Iijoki"],
    "Kokemäenjoki": ["Harjavalta", "Kolsi", "Äetsä", "Kokemäenjoki"],
    "Vuoksi": ["Imatra", "Tainionkoski", "Vuoksi"],
}
VATTEN = ("vattenkraft", "stromkraft", "pumpkraft")


def alv_for(namn):
    for alv, lista in ALVAR.items():
        if any(samma(namn, x) for x in lista):
            return alv
    return None


def parse_kapacitet_enheter(body):
    """14.1.B (A71/A33): installerad effekt per produktionsenhet. Returnerar [{namn, kraftslag, mw}]."""
    root = ET.fromstring(body)
    if localname(root) == "Acknowledgement_MarketDocument":
        return []
    out = []
    for ts in (e for e in root.iter() if localname(e) == "TimeSeries"):
        namn = None
        for e in ts.iter():
            ln = localname(e)
            if ln.endswith("registeredResource.name") and e.text:
                namn = e.text.strip(); break
        if not namn:
            for e in ts.iter():
                if localname(e) == "PowerSystemResources":
                    namn = find_text(e, "name"); break
        pts = [float(find_text(q, "quantity")) for q in ts.iter() if localname(q) == "Point" and find_text(q, "quantity")]
        if namn and pts:
            out.append({"namn": namn, "kraftslag": PSR.get(find_text(ts, "psrType"), "ovrigt"), "mw": round(pts[-1])})
    return out


def uppdatera_anlaggningar(now, budget_s=150):
    """Stora kraftverk per elområde med installerad effekt (14.1.B) till data/anlaggningar.json. En gång i veckan."""
    fil = OUT / "anlaggningar.json"
    old = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {}
    if old.get("uppdaterad") and datetime.now(timezone.utc) - datetime.strptime(old["uppdaterad"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) < timedelta(days=7) and len(old.get("zoner", {})) >= len(ZONES) - 1:
        return
    t0 = time.time()
    zoner = dict(old.get("zoner", {}))
    y = now.year
    for zone, code in ZONES.items():
        if time.time() - t0 > budget_s:
            print("anläggningar: tidsgränsen nådd, fortsätter nästa körning")
            break
        if zone in zoner and old.get("uppdaterad", "")[:4] == str(y) and datetime.now(timezone.utc) - datetime.strptime(old["uppdaterad"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) < timedelta(days=7):
            continue
        body = fetch_raw({"documentType": "A71", "processType": "A33", "in_Domain": code,
                          "periodStart": fmt(datetime(y, 1, 1, tzinfo=timezone.utc)), "periodEnd": fmt(datetime(y + 1, 1, 1, tzinfo=timezone.utc))})
        try:
            lista = parse_kapacitet_enheter(body)
        except ET.ParseError:
            lista = []
        if not lista:
            print(f"anläggningar {zone}: inget, svar: {ack_text(body)}")
        else:
            zoner[zone] = sorted(lista, key=lambda x: -x["mw"])
            print(f"anläggningar {zone}: {len(lista)} verk")
        time.sleep(0.5)
    out = {"uppdaterad": iso(datetime.now(timezone.utc)), "enhet": "MW", "zoner": zoner}
    fil.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def zon_for_enhet(namn, cc, anl):
    for zone, lista in anl.items():
        if land(zone) == cc and any(samma(namn, x["namn"]) for x in lista):
            return zone
    return None


def uppdatera_enheter(now, budget_s=150):
    """Senast publicerade produktion per kraftverk (A73) till data/enheter.json.
    Frågas per kontrollområde (land) och kopplas till elområde via anlaggningar.json.
    Publiceras med flera dagars fördröjning för vissa länder, därför letar vi bakåt dag för dag. Var tredje timme."""
    fil = OUT / "enheter.json"
    old = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {}
    if old.get("uppdaterad") and datetime.now(timezone.utc) - datetime.strptime(old["uppdaterad"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) < timedelta(hours=3):
        print("enheter: uppdaterade för mindre än tre timmar sedan, hoppar över")
        return
    af = OUT / "anlaggningar.json"
    anl = json.loads(af.read_text(encoding="utf-8")).get("zoner", {}) if af.exists() else {}
    t0 = time.time()
    zoner = dict(old.get("zoner", {}))
    lagar = dict(old.get("lag_land", {}))
    dag0 = now.replace(hour=0, minute=0)
    for cc, code in CONTROL.items():
        if time.time() - t0 > budget_s:
            print("enheter: tidsgränsen nådd, behåller gamla värden för resten")
            break
        hittat = None
        sista = ""
        for lag in range(max(0, lagar.get(cc, 0) - 1), 8):
            fran = dag0 - timedelta(days=lag)
            body = fetch_raw({"documentType": "A73", "processType": "A16", "in_Domain": code,
                              "periodStart": fmt(fran), "periodEnd": fmt(min(now, fran + timedelta(days=1)))})
            try:
                enh = parse_enheter(body)
            except ET.ParseError:
                enh = {}
            time.sleep(0.5)
            if enh:
                hittat = (lag, enh)
                break
            sista = ack_text(body)
        if not hittat:
            print(f"enheter {cc}: ingen data senaste veckan, svar: {sista}")
            continue
        lag, enh = hittat
        lagar[cc] = lag
        per_zon = {}
        for e in enh.values():
            z = zon_for_enhet(e["namn"], cc, anl) or (cc if cc in ZONES else None)
            if z:
                per_zon.setdefault(z, []).append(e)
        for z, lista in per_zon.items():
            lista = sorted(lista, key=lambda x: -x["mw"])[:60]
            zoner[z] = {"lag_dagar": lag, "data_tid": max(x["t"] for x in lista), "enheter": lista}
        print(f"enheter {cc}: {len(enh)} enheter, {lag} dagar bakåt, kopplade till {sorted(per_zon)}")
    out = {"uppdaterad": iso(datetime.now(timezone.utc)), "enhet": "MW", "lag_land": lagar, "zoner": zoner}
    fil.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


MAGASIN_ZONER = ["SE1", "SE2", "SE3", "SE4", "NO1", "NO2", "NO3", "NO4", "NO5", "FI"]


def parse_magasin(body):
    """16.1.D (A72): lagrad energi i vattenmagasin, veckovärden i MWh. Returnerar {veckostart_iso: mwh}."""
    root = ET.fromstring(body)
    if localname(root) == "Acknowledgement_MarketDocument":
        return {}
    out = {}
    for per in (e for e in root.iter() if localname(e) == "Period"):
        start = datetime.strptime(find_text(per, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        for q in per.iter():
            if localname(q) == "Point":
                out[iso(start + timedelta(days=7 * (int(find_text(q, "position")) - 1)))] = float(find_text(q, "quantity"))
    return out


def uppdatera_magasin(now, budget_s=150):
    """Magasinens fyllnad per zon (A72, veckovis) mot snittet för samma vecka de tre senaste åren. En gång per dygn."""
    fil = OUT / "magasin.json"
    old = json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else {}
    if old.get("uppdaterad") and datetime.now(timezone.utc) - datetime.strptime(old["uppdaterad"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) < timedelta(hours=20):
        return
    t0 = time.time()
    zoner = dict(old.get("zoner", {}))
    for zone in MAGASIN_ZONER:
        if time.time() - t0 > budget_s:
            print("magasin: tidsgränsen nådd, fortsätter nästa körning")
            break
        varden = {}
        for ar in range(0, 4):
            mitt = now - timedelta(days=365 * ar)
            body = fetch_raw({"documentType": "A72", "processType": "A16", "in_Domain": ZONES[zone],
                              "periodStart": fmt(mitt - timedelta(days=21)), "periodEnd": fmt(mitt + (timedelta(days=1) if ar == 0 else timedelta(days=7)))})
            try:
                v = parse_magasin(body)
            except ET.ParseError:
                v = {}
            if not v and ar == 0:
                print(f"magasin {zone}: inget, svar: {ack_text(body)}")
            varden[ar] = v
            time.sleep(0.5)
        if not varden.get(0):
            continue
        senaste = max(varden[0])
        nu = varden[0][senaste]
        t = datetime.strptime(senaste, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        jmf = []
        for ar in (1, 2, 3):
            mal = t - timedelta(days=364 * ar)  # samma veckodag ett år tidigare
            nara = [(abs((datetime.strptime(k, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) - mal).days), val) for k, val in varden.get(ar, {}).items()]
            nara = [x for x in nara if x[0] <= 4]
            if nara:
                jmf.append(min(nara)[1])
        snitt = sum(jmf) / len(jmf) if jmf else None
        zoner[zone] = {"vecka": senaste, "mwh": round(nu), "snitt_mwh": round(snitt) if snitt else None,
                       "andel_av_snitt": round(nu / snitt * 100) if snitt else None, "ar_i_snitt": len(jmf)}
        print(f"magasin {zone}: {round(nu)} MWh" + (f", {round(nu / snitt * 100)} % av snittet" if snitt else ""))
    out = {"uppdaterad": iso(datetime.now(timezone.utc)), "enhet": "MWh", "zoner": zoner}
    fil.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def uppdatera_vattenkraft():
    """Sammanfattning per älv (Sverige) eller per zon (övriga): installerat, tillgängligt nu (från avbrott)
    och senast publicerad produktion. Inga anrop, bara de andra filerna. Till data/vattenkraft.json."""
    def las(n):
        f = OUT / n
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    anl, avb, enh = las("anlaggningar.json").get("zoner", {}), las("avbrott.json").get("zoner", {}), las("enheter.json").get("zoner", {})
    nu = iso(datetime.now(timezone.utc))
    zoner = {}
    for zone in ZONES:
        verk = [v for v in anl.get(zone, []) if v["kraftslag"] in VATTEN]
        if not verk:
            continue
        outs = [a for a in avb.get(zone, []) if a.get("kraftslag") in VATTEN and a["start"] <= nu < a["slut"]]
        prod = (enh.get(zone) or {}).get("enheter", [])
        grupper = {}
        for v in verk:
            per_alv = zone.startswith("SE") or zone == "FI"
            g = (alv_for(v["namn"]) if per_alv else None) or ("Övriga stora verk" if per_alv else "Stora vattenkraftverk")
            x = grupper.setdefault(g, {"alv": g, "verk": 0, "nominell_mw": 0, "tillganglig_mw": 0, "avbrott": [], "produktion_mw": 0, "med_produktion": 0})
            x["verk"] += 1
            x["nominell_mw"] += v["mw"]
            mine = [a for a in outs if samma(a["namn"], v["namn"])]
            tillg = v["mw"]
            if mine:
                a = min(mine, key=lambda a: a.get("tillganglig_mw") or 0)
                tillg = min(v["mw"], a.get("tillganglig_mw") or 0)
                x["avbrott"].append({"namn": v["namn"], "tillganglig_mw": a.get("tillganglig_mw"), "nominell_mw": v["mw"],
                                     "slut": a["slut"], "orsak": a.get("orsak")})
            x["tillganglig_mw"] += tillg
            p = [e for e in prod if samma(e["namn"], v["namn"])]
            if p:
                x["produktion_mw"] += sum(e["mw"] for e in p)
                x["med_produktion"] += 1
        lista = sorted(grupper.values(), key=lambda g: (g["alv"].startswith("Övriga"), -g["nominell_mw"]))
        zoner[zone] = {"produktion_tid": (enh.get(zone) or {}).get("data_tid"), "alvar": lista}
    (OUT / "vattenkraft.json").write_text(json.dumps({"uppdaterad": nu, "enhet": "MW", "zoner": zoner}, ensure_ascii=False, indent=1), encoding="utf-8")


def ack_text(body):
    try:
        root = ET.fromstring(body)
        return " ".join(e.text.strip() for e in root.iter() if localname(e) == "text" and e.text)[:300] or body[:200]
    except Exception:
        return repr(body[:200])


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
    t_start = time.time()
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

    # Extra: avbrott för alla kraftslag och produktion per kraftverk. Körs sist så att priser och
    # produktion alltid hinner sparas, och bara om körningen har tid kvar (arbetsflödet har 12 min).
    SLUT = 480  # sekunder från start som de extra delarna får använda
    for namn, fn in (("avbrott", uppdatera_avbrott), ("magasin", uppdatera_magasin),
                     ("anläggningar", uppdatera_anlaggningar), ("enheter", uppdatera_enheter)):
        kvar = SLUT - (time.time() - t_start)
        if kvar < 45:
            print(f"{namn}: för lite tid kvar ({int(kvar)} s), tar det nästa körning")
            continue
        try:
            fn(now, budget_s=min(150, kvar - 30))
        except RateLimit:
            print(f"429 vid {namn}, försöker nästa körning")
        except Exception as e:
            print(f"{namn}: fel {e!r}")
    try:
        uppdatera_vattenkraft()
    except Exception as e:
        print(f"vattenkraft: fel {e!r}")


if __name__ == "__main__":
    main()
