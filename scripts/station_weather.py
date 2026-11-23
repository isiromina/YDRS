"""Publish precipitation only, using public stations and configured forecast locations."""
import csv
import json
import math
import os
import re
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
            if exc.code == 403 and endpoint.endswith('/stn_inf.php'):
                body = exc.read(8192)
                detail = ' '.join(body.decode(enc, errors='ignore').lower() for enc in ('utf-8', 'cp949'))
                reasons = [('subscription', ('활용신청', '활용 신청', '신청하지', 'not subscribed', 'subscription')),
                           ('credential', ('인증키', 'authkey', 'invalid key')),
                           ('source IP', ('ip address', '허용되지 않은 ip')),
                           ('quota', ('quota', '호출 횟수'))]
                category = next((label for label, words in reasons if any(word in detail for word in words)), 'unspecified')
                raise SystemExit(f'HTTP 403 ({category})') from None
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
    stations = {}
    try:
        catalog = fetch('https://apihub.kma.go.kr/api/typ01/url/stn_inf.php',
                        dict(inf='RN', tm=stamp, help=0, authKey=os.environ['KMA_KEY']))
    except SystemExit as exc:
        print('Gauge catalog unavailable:', str(exc))
    else:
        for line in catalog.decode('cp949', errors='replace').splitlines():
            parts = line.split()
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            try:
                lon, lat = map(float, parts[1:3])
            except ValueError:
                continue
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                stations[parts[0]] = (lat, lon)
    for row in rows:
        if row['station_id'] in stations:
            row['station_lat'], row['station_lon'] = stations[row['station_id']]
    print('Observations with gauge coordinates:', sum('station_lat' in row for row in rows))
    return dict(source='KMA AWS', observed_at=when.isoformat(), records=rows)


def forecasts():
    raw = os.environ.get('WEATHER_POINTS', '').strip()
    points = json.loads(raw) if raw else {'P000': [51.51, -0.13]}
    if not isinstance(points, dict) or not points:
        raise SystemExit('Invalid forecast point configuration')
    rows = []
    for code, point in points.items():
        if (not re.fullmatch(r'P[0-9]{3,5}', code) or not isinstance(point, list)
                or len(point) != 2 or any(type(v) not in (int, float) for v in point)
                or not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180):
            raise SystemExit('Invalid forecast point configuration')
        data = fetch('https://api.weatherapi.com/v1/forecast.json',
                     dict(q=f'{point[0]},{point[1]}', days=3, alerts='no', aqi='no',
                          key=os.environ['WEATHER_KEY']))
        for day in json.loads(data)['forecast']['forecastday']:
            for hour in day['hour']:
                amount = rainfall(hour['precip_mm'])
                if amount is None:
                    raise SystemExit('Invalid forecast precipitation')
                when = datetime.fromtimestamp(hour['time_epoch'], UTC)
                rows.append(dict(point_id=code, valid_at=when.isoformat(), precip_mm=amount))
    result = dict(source='WeatherAPI', records=rows)
    if not raw:
        result['location'] = 'London'
    return result


feed = sys.argv[1]
snapshot = {'kma-aws': observations, 'weatherapi': forecasts}[feed]()
if not snapshot['records']:
    raise SystemExit('Empty snapshot')
snapshot['collected_at'] = datetime.now(UTC).isoformat()
output = Path('output')
output.mkdir(exist_ok=True)
(output / (feed + '.json')).write_text(json.dumps(snapshot, separators=(',', ':'), allow_nan=False))
print('Validated records:', len(snapshot['records']))
