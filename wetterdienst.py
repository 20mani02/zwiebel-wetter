#!/usr/bin/env python3
"""
Zwiebel-Wetterdienst
====================
Laedt die Lokalprognosen von MeteoSchweiz (Open Data, Lizenz CC BY 4.0) und bereitet
sie fuer die App pro Postleitzahl auf.

    python3 wetterdienst.py               # einmal laden
    python3 wetterdienst.py --stuendlich  # laden und danach jede Stunde wieder

Ergebnis im Ordner «daten» neben diesem Skript:
    daten/stand.json      Stand, Modelllauf, Liste der Gruppen
    daten/orte.json       alle Postleitzahl-Orte fuer die Ortssuche
    daten/g39.json …      Prognosen, gruppiert nach den ersten Ziffern der Postleitzahl

Quelle: MeteoSchweiz. Die Daten werden ausgewaehlt und umgerechnet (bearbeitet).
Nur Python-Standardbibliothek (ab Python 3.9), keine Installation noetig.
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

FORMAT = 2
COLLECTION = "ch.meteoschweiz.ogd-local-forecasting"
STAC_ITEM = "https://data.geo.admin.ch/api/stac/v1/collections/" + COLLECTION + "/items/{}-ch"
META_POINT = "https://data.geo.admin.ch/" + COLLECTION + "/ogd-local-forecasting_meta_point.csv"

# Hourly parameters -> (key in the output, hours between the time stamp and the hour the value is used for).
# MeteoSchweiz stamps the END of each interval (UTC):
#   tre200h0 / fu3010h0 are hourly means, the value stamped 08:00 belongs to 07:00-08:00 -> hour 7.
#   rp0003i0 / jww003i0 cover the preceding 3 hours; the window 06:00-09:00 (stamp 09:00) is centred
#   on hour 7, so a commute at 7 o'clock sees the rain chance from one hour before to two hours after.
HOURLY = {
    "tre200h0": ("t", 1),   # temperature 2 m, deg C
    "fu3010h0": ("w", 1),   # wind speed, km/h
    "rp0003i0": ("p", 2),   # probability of precipitation in 3 h, %
    "jww003i0": ("s", 2),   # MeteoSchweiz weather symbol for 3 h (1-42 day, 101-142 night)
}
# Daily parameters, stored per day as [min, max, symbol, mm, max rain probability 06-22]
DAILY = ["tre200pn", "tre200px", "jp2000d0", "rka150p0"]

TZ = ZoneInfo("Europe/Zurich")
HOURS = list(range(6, 23))       # hourly values 06-22 local time (as in the app)
DAYS = 9                         # MeteoSchweiz forecasts 9 days including today
HOURLY_DAYS = 4                  # hourly values for today and the next 3 days, daily values after that
MAX_DOC = 240_000                # bytes per group document (database limit 256 KiB)
UA = "Zwiebel-Wetterdienst/1.1 (Prototyp)"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def get(url, timeout=120):
    """Download as bytes. curl if available (uses the system certificates on macOS), else urllib."""
    curl = shutil.which("curl")
    if curl:
        r = subprocess.run([curl, "-fsSL", "--compressed", "--retry", "2", "-A", UA, "--max-time", str(timeout), url],
                           capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("Download fehlgeschlagen ({}): {} {}".format(
                r.returncode, url, r.stderr.decode("utf-8", "replace").strip()[:200]))
        return r.stdout
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        return gzip.decompress(data) if resp.headers.get("Content-Encoding") == "gzip" else data


def utc(stamp):
    return dt.datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)


def latest_run():
    """Newest model run that has all needed files. STAC items are named after the UTC date."""
    need = set(HOURLY) | set(DAILY)
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(3):
        day = (now - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            item = json.loads(get(STAC_ITEM.format(day), timeout=60))
        except Exception as e:  # noqa: BLE001 - try the day before
            log("Katalog {}: {}".format(day, e))
            continue
        runs = {}
        for key, asset in item.get("assets", {}).items():
            parts = key.split(".")      # vnut12.lssw.YYYYMMDDHHmm.<parameter>.csv
            if len(parts) >= 5 and parts[0] == "vnut12":
                runs.setdefault(parts[2], {})[parts[3]] = asset["href"]
        complete = sorted(r for r, a in runs.items() if need <= set(a))
        if complete:
            return complete[-1], runs[complete[-1]]
    raise RuntimeError("Kein vollstaendiger Modelllauf bei MeteoSchweiz gefunden.")


def rows(data):
    """CSV rows (point_id, point_type_id, Date, value) of a MeteoSchweiz data file."""
    reader = csv.reader(io.StringIO(data.decode("latin-1")), delimiter=";")
    header = [h.strip().lower() for h in next(reader)]
    ip, it = header.index("point_id"), header.index("point_type_id")
    idate = next(i for i, h in enumerate(header) if h in ("date", "time"))
    ival = len(header) - 1
    for r in reader:
        if len(r) > ival:
            yield r[ip].strip(), r[it].strip(), r[idate].strip(), r[ival]


def num(v, digits=0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, digits) if digits else int(round(f))


def load_points():
    """Places with a postal code: (point_type_id, point_id) -> (plz, name, lat, lon)."""
    reader = csv.reader(io.StringIO(get(META_POINT).decode("latin-1")), delimiter=";")
    header = [h.strip() for h in next(reader)]
    col = {h: i for i, h in enumerate(header)}

    def find(*parts):
        for part in parts:
            for h, i in col.items():
                if part in h:
                    return i
        return None

    i_id, i_type, i_plz, i_name = col["point_id"], col["point_type_id"], col["postal_code"], col["point_name"]
    i_lat, i_lon = find("wgs84_lat", "_lat"), find("wgs84_lon", "_lon")
    pts = {}
    for r in reader:
        if len(r) <= max(i_id, i_type, i_plz, i_name):
            continue
        plz = r[i_plz].strip().split(".")[0]
        if not plz.isdigit():
            continue                        # stations and mountain points have no postal code
        name = re.sub(r"^\d{4}\s+", "", r[i_name].strip())
        lat = num(r[i_lat], 4) if i_lat is not None and i_lat < len(r) else None
        lon = num(r[i_lon], 4) if i_lon is not None and i_lon < len(r) else None
        pts[(r[i_type].strip(), r[i_id].strip())] = (plz.zfill(4), name, lat, lon)
    if not pts:
        raise RuntimeError("Keine Postleitzahl-Orte in der Punktliste gefunden.")
    return pts


def build(out_dir):
    t0 = time.time()
    run, hrefs = latest_run()
    run_utc = utc(run)
    log("Modelllauf {:%d.%m.%Y %H:%M} Uhr".format(run_utc.astimezone(TZ)))
    old = os.path.join(out_dir, "stand.json")
    if os.path.exists(old):
        try:
            with open(old, encoding="utf-8") as fh:
                if json.load(fh).get("run") == run:
                    log("Keine neuen Daten, Stand bleibt.")
                    return None
        except (OSError, ValueError):
            pass

    points = load_points()
    types = {t for t, _ in points}
    # Record id: the point id (unique within its type); prefixed only if several types carry postal codes
    ids = {(t, p): (p if len(types) == 1 else t + "_" + p) for t, p in points}
    log("{} Postleitzahl-Orte".format(len(points)))

    d0 = run_utc.astimezone(TZ).date()
    dates = [d0 + dt.timedelta(days=i) for i in range(DAYS)]
    day_ix = {d: i for i, d in enumerate(dates)}
    hour_ix = {h: i for i, h in enumerate(HOURS)}
    cache = {}

    def slot(stamp, shift):
        """UTC stamp -> (day index, hour index) in local time after the shift, or False."""
        s = cache.get((stamp, shift))
        if s is None:
            local = (utc(stamp) - dt.timedelta(hours=shift)).astimezone(TZ)
            di, hi = day_ix.get(local.date()), hour_ix.get(local.hour)
            s = cache[(stamp, shift)] = (di, hi) if di is not None and hi is not None and local.minute == 0 else False
        return s

    hourly = {key: {} for key, _ in HOURLY.values()}
    pmax = {}                                   # rid -> highest rain probability 06-22 per day, all 9 days
    for p, (key, shift) in HOURLY.items():
        data = get(hrefs[p], timeout=300)
        log("{}: {:.1f} MB".format(p, len(data) / 1e6))
        target = hourly[key]
        for pid, ptype, stamp, val in rows(data):
            rid = ids.get((ptype, pid))
            if rid is None:
                continue
            s = slot(stamp, shift)
            v = num(val) if s else None
            if v is None:
                continue
            di, hi = s
            if key == "p":
                m = pmax.setdefault(rid, [None] * DAYS)
                if m[di] is None or v > m[di]:
                    m[di] = v
            if di < HOURLY_DAYS:
                arr = target.get(rid)
                if arr is None:
                    arr = target[rid] = [[None] * len(HOURS) for _ in range(HOURLY_DAYS)]
                arr[di][hi] = v
        del data

    daily = {}
    for k, p in enumerate(DAILY):
        data = get(hrefs[p], timeout=120)
        for pid, ptype, stamp, val in rows(data):
            rid = ids.get((ptype, pid))
            if rid is None:
                continue
            di = day_ix.get(utc(stamp).astimezone(TZ).date())   # daily rows are stamped 00:00 UTC of the day
            if di is None:
                continue
            rec = daily.setdefault(rid, [[None] * 5 for _ in range(DAYS)])
            rec[di][k] = num(val) if p == "jp2000d0" else num(val, 1)
    for rid, m in pmax.items():
        rec = daily.setdefault(rid, [[None] * 5 for _ in range(DAYS)])
        for di, v in enumerate(m):
            rec[di][4] = v

    # One record per place: n, z, d = [[min, max, symbol, mm, rain %] x 9 days], t/w/p/s = 4 days x hours 06-22
    groups = {}
    for (ptype, pid), (plz, name, lat, lon) in points.items():
        rid = ids[(ptype, pid)]
        rec = {"n": name, "z": plz, "d": daily.get(rid, [])}
        for key in hourly:
            if rid in hourly[key]:
                rec[key] = hourly[key][rid]
        groups.setdefault(plz[:2], {})[rid] = rec

    # Write into a new folder first and swap at the end, so a reader never sees half a run
    tmp = out_dir.rstrip(os.sep) + ".neu"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    meta = {"run": run, "d0": d0.isoformat(), "stunden": HOURS[0], "stundenTage": HOURLY_DAYS, "format": FORMAT}
    gmap, files = {}, []

    def write(name, obj):
        path = os.path.join(tmp, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        files.append((name, os.path.getsize(path)))

    for prefix in sorted(groups):
        pts = groups[prefix]
        body = json.dumps({**meta, "pts": pts}, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) <= MAX_DOC:
            gmap[prefix] = "g" + prefix
            write("g" + prefix, {**meta, "pts": pts})
        else:  # too big for one database document: split by the third digit
            sub = {}
            for rid, rec in pts.items():
                sub.setdefault(rec["z"][:3], {})[rid] = rec
            for p3 in sorted(sub):
                gmap[p3] = "g" + p3
                write("g" + p3, {**meta, "pts": sub[p3]})

    orte = sorted(([ids[k], plz, name, lat, lon] for k, (plz, name, lat, lon) in points.items()),
                  key=lambda o: (o[2].lower(), o[1]))
    write("orte", {**meta, "o": orte})
    stand = {**meta, "coll": "msw_" + run, "lauf": run_utc.isoformat(),
             "erstellt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "gruppen": gmap, "orte": len(points), "quelle": "MeteoSchweiz", "lizenz": "CC BY 4.0",
             "hinweis": "Quelle: MeteoSchweiz. Daten ausgewaehlt und umgerechnet."}
    # Upload batches for the prototype's database (at most ~0.9 MB and 45 documents each)
    batches, cur, size = [], [], 0
    for n, s in sorted(files, key=lambda x: -x[1]):
        if cur and (size + s > 900_000 or len(cur) >= 45):
            batches.append(cur)
            cur, size = [], 0
        cur.append(n)
        size += s
    if cur:
        batches.append(cur)
    stand["batches"] = batches
    write("stand", stand)

    if os.path.isdir(out_dir):
        old_dir = out_dir.rstrip(os.sep) + ".alt"
        shutil.rmtree(old_dir, ignore_errors=True)
        os.rename(out_dir, old_dir)
        os.rename(tmp, out_dir)
        shutil.rmtree(old_dir, ignore_errors=True)
    else:
        os.rename(tmp, out_dir)
    total = sum(s for _, s in files)
    log("Fertig in {:.0f} s: {} Gruppen, {:.1f} MB in {}".format(time.time() - t0, len(gmap), total / 1e6, out_dir))
    return stand


def main():
    ap = argparse.ArgumentParser(description="Lokalprognosen von MeteoSchweiz fuer Zwiebel aufbereiten")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "daten"))
    ap.add_argument("--stuendlich", action="store_true", help="nach dem Laden jede Stunde wiederholen (Fenster offen lassen)")
    args = ap.parse_args()
    while True:
        try:
            build(args.out)
        except Exception as e:  # noqa: BLE001 - keep the hourly loop alive
            log("Fehler: {}".format(e))
            if not args.stuendlich:
                sys.exit(1)
        if not args.stuendlich:
            break
        now = dt.datetime.now()
        nxt = (now + dt.timedelta(hours=1)).replace(minute=20, second=0, microsecond=0)
        if nxt - now > dt.timedelta(minutes=70):
            nxt -= dt.timedelta(hours=1)
        log("Naechstes Update um {:%H:%M} Uhr".format(nxt))
        time.sleep(max(60, (nxt - now).total_seconds()))


if __name__ == "__main__":
    main()
