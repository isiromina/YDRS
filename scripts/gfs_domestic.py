"""Extract hourly APCP for configured points from one NOAA GFS cycle."""
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from eccodes import codes_grib_new_from_file, codes_get, codes_get_elements, codes_release
from public_feeds import read, iso


def sample(run, lead, points):
    stem = f'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p25.f{lead:03d}'
    lines = read(stem+'.idx').decode().splitlines()
    for i, line in enumerate(lines[:-1]):
        match = re.search(r':APCP:surface:(\d+)-(\d+) hour acc fcst:', line)
        if not match or int(match[2]) != lead:
            continue
        body = read(stem, (int(line.split(':')[1]), int(lines[i+1].split(':')[1])-1))
        with tempfile.TemporaryFile() as f:
            f.write(body)
            f.seek(0)
            gid = codes_grib_new_from_file(f)
            try:
                if (codes_get(gid,'Ni'), codes_get(gid,'Nj'), codes_get(gid,'scanningMode'),
                    codes_get(gid,'latitudeOfFirstGridPointInDegrees'),codes_get(gid,'longitudeOfFirstGridPointInDegrees')) != (1440,721,0,90,0):
                    raise ValueError('Unexpected GFS grid')
                idx = [round((90-lat)*4)*1440+round((lon%360)*4)%1440 for lat,lon in points.values()]
                return int(match[1]), list(codes_get_elements(gid,'values',idx))
            finally:
                codes_release(gid)
    raise ValueError('Missing precipitation record')


def main():
    now = datetime.now(timezone.utc)
    points = {k:v for k,v in json.loads(os.environ['WEATHER_POINTS']).items()
              if re.fullmatch(r'P\d{3,5}',k) and -90<=v[0]<=90 and -180<=v[1]<=180}
    hour = now.replace(minute=0, second=0, microsecond=0)
    run = hour.replace(hour=hour.hour//6*6)
    # Test the last required file before downloading a partially published cycle.
    for attempt in range(4):
        first = int((hour-run).total_seconds()/3600)+1
        try:
            sample(run,first+55,points)
            break
        except HTTPError as exc:
            if exc.code not in (403,404): raise
            run -= timedelta(hours=6)
    else:
        raise ValueError('No complete cycle')
    result = {k:{} for k in points}
    previous = None
    for lead in range(first,first+56):
        start, values = sample(run,lead,points)
        if start < lead-1 and previous is None:
            previous = sample(run,lead-1,points)
        if start != lead-1 and (previous is None or previous[0]!=start):
            raise ValueError('Incompatible accumulation intervals')
        end = str(int((run+timedelta(hours=lead)).timestamp()))
        for i,key in enumerate(points):
            mm = values[i] if start==lead-1 else values[i]-previous[1][i]
            if not math.isfinite(mm) or mm < -0.2 or mm>500: raise ValueError('Invalid hourly amount')
            result[key][end] = round(max(0,mm),2)
        previous = start,values
    path = Path('output/gfs-domestic.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(source='NOAA GFS',issued_at=iso(run),
        collected_at=iso(datetime.now(timezone.utc)),points=result),separators=(',',':')))


if __name__ == '__main__':
    main()
