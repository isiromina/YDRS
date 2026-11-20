"""Produce a public GDACS event list or a global, one-hour GFS forecast snapshot."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import json
import math
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

UTC = timezone.utc


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read(url, byte_range=None):
    headers = {"User-Agent": "YDRS-public-data/1.0"}
    if byte_range:
        headers["Range"] = "bytes=%d-%d" % byte_range
    with urlopen(Request(url, headers=headers), timeout=45) as response:
        if byte_range and response.status != 206:
            raise ValueError("Source did not honor byte range")
        body = response.read(12_000_001)
    if len(body) > 12_000_000:
        raise ValueError("Oversized response")
    return body


def gdacs_time(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        dt = parsedate_to_datetime(value)
    return iso(dt if dt.tzinfo else dt.replace(tzinfo=UTC))


def disasters():
    root = ET.fromstring(read("https://www.gdacs.org/xml/rss.xml"))
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Invalid GDACS RSS")
    rows = []
    for item in root.findall("channel/item"):
        fields = {el.tag.rsplit("}", 1)[-1]: (el.text or "").strip() for el in item}
        kind, eid = fields["eventtype"], fields["eventid"]
        if not re.fullmatch("[A-Z]{2}", kind) or not re.fullmatch("[0-9]+", eid):
            raise ValueError("Invalid event identity")
        lat, lon = float(fields["lat"]), float(fields["long"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinates")
        row = dict(event_type=kind, event_id=eid, latitude=lat, longitude=lon,
                   started_at=gdacs_time(fields["fromdate"]), updated_at=gdacs_time(fields["pubDate"]))
        level = fields.get("alertlevel", "").lower()
        if level in {"green", "orange", "red"}:
            row["alert_level"] = level
        rows.append(row)
    return dict(schema="ydrs.gdacs.v1", source="GDACS", source_url="https://www.gdacs.org/", events=rows)


def accumulation(cycle, lead, directory):
    stem = f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{lead:03d}"
    lines = read(stem + ".idx").decode().splitlines()
    for i, line in enumerate(lines[:-1]):
        match = re.search(r":APCP:surface:(\d+)-(\d+) hour acc fcst:", line)
        if match and int(match[2]) == lead:
            bounds = (int(line.split(":")[1]), int(lines[i+1].split(":")[1])-1)
            path = directory / f"precip-{lead}.grib2"
            path.write_bytes(read(stem, bounds))
            # Select one grid point per degree before building the published grid.
            proc = subprocess.Popen(["grib_get_data", "-F", "%.4f", str(path)], stdout=subprocess.PIPE, text=True)
            grid = {}
            with proc.stdout as stream:
                next(stream)
                for record in stream:
                    lat, lon, value = map(float, record.split())
                    if lat.is_integer() and lon.is_integer():
                        grid[int(lat), (int(lon)+180) % 360-180] = value
            if proc.wait() or len(grid) != 181*360:
                raise ValueError("Incomplete GFS grid")
            return int(match[1]), grid
    raise ValueError("Missing APCP forecast interval")


def weather(now):
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    cycle = end.replace(hour=(end.hour//6)*6)
    with tempfile.TemporaryDirectory() as tmp:
        for back in range(4):
            run = cycle-timedelta(hours=6*back)
            lead = int((end-run).total_seconds()//3600)
            if lead == 0:
                continue
            try:
                start, values = accumulation(run, lead, Path(tmp))
                if start != lead-1:
                    start_prev, prev = accumulation(run, lead-1, Path(tmp))
                    if start_prev != start:
                        raise ValueError("Incompatible GFS accumulation intervals")
                    values = {key: val-prev[key] for key, val in values.items()}
            except HTTPError as exc:
                if exc.code in (403, 404):
                    continue
                raise
            if any(not math.isfinite(v) or v < -0.2 for v in values.values()):
                raise ValueError("Invalid hourly difference")
            # Global cells centered at half degrees, average the four corner samples.
            # This is a coarse forecast product, not a station observation.
            grid = [[round(sum(max(0, values[y, x]) for y in (lat, lat-1)
                               for x in (lon, (lon+181)%360-180))/4, 2)
                     for lon in range(-180,180)] for lat in range(90,-90,-1)]
            return dict(schema="ydrs.precipitation-grid.v1", source="NOAA GFS", model_run=iso(run),
                        valid_at=iso(end), period_hours=1, unit="mm", crs="EPSG:4326",
                        row_order="north_to_south", ncols=360, nrows=180, xllcorner=-180,
                        yllcorner=-90, cellsize=1, values=grid)
    raise RuntimeError("No available GFS cycle")


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind not in {"weather", "disasters"}:
        raise SystemExit("Unknown feed")
    now = datetime.now(UTC)
    result = weather(now) if kind == "weather" else disasters()
    result["collected_at"] = iso(datetime.now(UTC))
    Path("output").mkdir(exist_ok=True)
    Path(f"output/{kind}.json").write_text(json.dumps(result, allow_nan=False, separators=(",", ":")))
