"""Publish precipitation only, using public stations and the London pilot forecast."""
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(endpoint, params):
    request = Request(endpoint + '?' + urlencode(params), headers={'User-Agent': 'YDRS/1.0'})
    for attempt in range(3):
        try:
            with build_opener(NoRedirect()).open(request, timeout=40) as response:
                data = response.read(512001)
                if len(data) > 512000:
                    raise SystemExit('Response exceeds size limit')
                return data
        except HTTPError as exc:
            if exc.code not in (502, 503, 504) or attempt == 2:
                raise SystemExit(f'Provider returned HTTP {exc.code}') from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise SystemExit('Provider connection failed') from None
        time.sleep(5 * (attempt + 1))


def rainfall(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def observations():
    when = (datetime.now(KST) - timedelta(minutes=15)).replace(second=0, microsecond=0)
    stamp = when.strftime('%Y%m%d%H%M')
    data = fetch('https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min_o',
                 dict(tm2=stamp, stn=0, disp=1, help=0, authKey=os.environ['KMA_KEY']))
    rows = []
    for line in csv.reader(data.decode('cp949', errors='replace').splitlines()):
        fields = [field.strip().rstrip('=') for field in line]
        if len(fields) < 8 or fields[0] != stamp or not fields[1].isdigit():
            continue
        hour = rainfall(fields[5])
        if hour is not None:
            rows.append(dict(station_id=fields[1], rain_60m_mm=hour,
                             rain_15m_mm=rainfall(fields[4]), rain_12h_mm=rainfall(fields[6]),
                             rain_day_mm=rainfall(fields[7])))
    return dict(source='KMA AWS', observed_at=when.isoformat(), records=rows)


def forecasts():
    data = fetch('https://api.weatherapi.com/v1/forecast.json',
                 dict(q='London', days=1, alerts='no', aqi='no', key=os.environ['WEATHER_KEY']))
    days = json.loads(data)['forecast']['forecastday']
    rows = []
    for day in days:
        for hour in day['hour']:
            amount = rainfall(hour['precip_mm'])
            if amount is None:
                raise SystemExit('Invalid forecast precipitation')
            when = datetime.fromtimestamp(hour['time_epoch'], UTC)
            rows.append(dict(valid_at=when.isoformat(), precip_mm=amount))
    return dict(source='WeatherAPI', location='London', records=rows)


feed = sys.argv[1]
snapshot = {'kma-aws': observations, 'weatherapi': forecasts}[feed]()
if not snapshot['records']:
    raise SystemExit('Empty snapshot')
snapshot['collected_at'] = datetime.now(UTC).isoformat()
output = Path('output')
output.mkdir(exist_ok=True)
(output / (feed + '.json')).write_text(json.dumps(snapshot, separators=(',', ':'), allow_nan=False))
print('Validated records:', len(snapshot['records']))
