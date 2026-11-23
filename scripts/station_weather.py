"""Publish precipitation only, using public stations and configured forecast locations."""
import csv
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(endpoint, params, limit=512000):
    url = endpoint + '?' + urlencode(params) if params else endpoint
    request = Request(url, headers={'User-Agent': 'YDRS/1.0'})
    for attempt in range(3):
        try:
            with build_opener(NoRedirect()).open(request, timeout=40) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
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


class ObservationTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.cells = []
        self.value = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.cells = []
        elif tag in ('td', 'th'):
            self.value = []

    def handle_data(self, data):
        if self.value is not None:
            self.value.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.value is not None:
            self.cells.append(''.join(self.value).strip())
            self.value = None
        if tag == 'tr' and self.cells:
            self.rows.append(self.cells)
            self.cells = []


def station_catalog(network, stamp):
    stations = {}
    try:
        catalog = fetch('https://apihub.kma.go.kr/api/typ01/url/stn_inf.php',
                        dict(inf=network, tm=stamp, help=0, authKey=os.environ['KMA_KEY']))
    except SystemExit as exc:
        print(network, 'catalog unavailable:', str(exc))
    else:
        for line in catalog.decode('cp949', errors='replace').splitlines():
            parts = line.split()
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            try:
                lon, lat = map(float, parts[1:3])
            except ValueError:
                continue
            if 124 <= lon <= 132 and 32 <= lat <= 40:
                stations[parts[0]] = (lat, lon)
    return stations


def gauge_rows(stamp):
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
    stations = station_catalog('RN', stamp)
    for row in rows:
        if row['station_id'] in stations:
            row['station_lat'], row['station_lon'] = stations[row['station_id']]
    return rows


def public_observations():
    data = fetch('https://www.weather.go.kr/cgi-bin/aws/nph-aws_txt_min', {}, limit=6000000)
    html = data.decode('cp949', errors='replace')
    match = re.search(r'\d{4}\.\d{2}\.\d{2}\.\d{2}:\d{2}', html)
    if not match:
        raise SystemExit('Missing KMA table observation time')
    when = datetime.strptime(match.group(), '%Y.%m.%d.%H:%M').replace(tzinfo=KST)
    if not timedelta(0) <= datetime.now(KST) - when <= timedelta(hours=1):
        raise SystemExit('Stale KMA table')
    table = ObservationTable()
    table.feed(html)
    header = ['강수15', '강수60', '강수3H', '강수6H', '강수12H', '일강수']
    if not any(row[2:8] == header for row in table.rows):
        raise SystemExit('Unrecognized KMA rainfall columns')
    weather_header = ['기온', '풍향1', '풍속1(m/s)', '풍향10', '풍속10(m/s)', '습도', '해면기압']
    has_weather = any(row[8:15] == weather_header for row in table.rows)
    # Public ASOS metadata from https://minwon.kma.go.kr/main/obvStn.do
    with Path(__file__).with_name('kma_asos_coordinates.csv').open() as stream:
        stations = {sid: (float(lat), float(lon)) for sid, lat, lon in csv.reader(stream)}
    stations.update(station_catalog('AWS', when.strftime('%Y%m%d%H%M')))
    rows = []
    for fields in table.rows:
        if len(fields) < 10 or fields[0] not in stations:
            continue
        hour = rainfall(fields[5])
        if hour is None:
            continue
        lat, lon = stations[fields[0]]
        rows.append(dict(station_id=fields[0], station_lat=lat, station_lon=lon,
                         rain_15m_mm=rainfall(fields[4]), rain_60m_mm=hour,
                         rain_3h_mm=rainfall(fields[6]), rain_6h_mm=rainfall(fields[7]),
                         rain_12h_mm=rainfall(fields[8]), rain_day_mm=rainfall(fields[9])))
        if has_weather and len(fields) >= 19:
            for key, index, low, high in [
                ('temperature_c', 10, -90, 65), ('wind_direction_deg', 14, 0, 360),
                ('wind_speed_ms', 16, 0, 150), ('humidity_pct', 17, 0, 100),
                ('sea_level_pressure_hpa', 18, 800, 1100),
            ]:
                try:
                    value = float(fields[index])
                except ValueError:
                    continue
                if low <= value <= high:
                    rows[-1][key] = value
    if not rows:
        raise SystemExit('No geolocated public observations')
    return when, rows


def observations():
    try:
        when, rows = public_observations()
    except SystemExit as exc:
        print('Public station table unavailable:', str(exc))
        when = (datetime.now(KST) - timedelta(minutes=15)).replace(second=0, microsecond=0)
        rows = []
    try:
        gauges = gauge_rows(when.strftime('%Y%m%d%H%M'))
    except SystemExit as exc:
        if not rows:
            raise
        print('Gauge observations unavailable:', str(exc))
    else:
        existing = {row['station_id'] for row in rows}
        rows.extend(row for row in gauges if row['station_id'] not in existing)
    print('Observations with station coordinates:', sum('station_lat' in row for row in rows))
    return dict(source='KMA AWS', observed_at=when.isoformat(), records=rows)


def forecasts():
    raw = os.environ.get('WEATHER_POINTS', '').strip()
    points = json.loads(raw) if raw else {'P000': [51.51, -0.13]}
    if not isinstance(points, dict) or not points:
        raise SystemExit('Invalid forecast point configuration')
    rows = []
    current_by_point = {}
    for code, point in points.items():
        if (not re.fullmatch(r'P[0-9]{3,5}', code) or not isinstance(point, list)
                or len(point) != 2 or any(type(v) not in (int, float) for v in point)
                or not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180):
            raise SystemExit('Invalid forecast point configuration')
        data = fetch('https://api.weatherapi.com/v1/forecast.json',
                     dict(q=f'{point[0]},{point[1]}', days=3, alerts='no', aqi='no',
                          key=os.environ['WEATHER_KEY']))
        forecast = json.loads(data)
        current = forecast.get('current', {})
        values = {field: current[field] for field in
                  ('temp_c', 'humidity', 'wind_kph', 'wind_degree', 'pressure_mb', 'cloud',
                   'last_updated_epoch')
                  if type(current.get(field)) in (int, float) and math.isfinite(current[field])}
        condition = current.get('condition', {}).get('text')
        if isinstance(condition, str):
            values['condition'] = {'text': condition[:100]}
        current_by_point[code] = values
        for day in forecast['forecast']['forecastday']:
            for hour in day['hour']:
                amount = rainfall(hour['precip_mm'])
                if amount is None:
                    raise SystemExit('Invalid forecast precipitation')
                when = datetime.fromtimestamp(hour['time_epoch'], UTC)
                rows.append(dict(point_id=code, valid_at=when.isoformat(), precip_mm=amount))
    result = dict(source='WeatherAPI', records=rows, current_by_point=current_by_point)
    if not raw:
        result['location'] = 'London'
    return result


def retain_hourly_estimates(snapshot):
    issued = datetime.fromisoformat(snapshot['collected_at']).timestamp()
    cutoff = issued - 73 * 3600
    path = Path('feeds/weatherapi.json')
    previous = json.loads(path.read_text()) if path.exists() else {}
    history = {}
    for row in snapshot['records']:
        code = row['point_id']
        if code not in history:
            prior = previous.get('hourly_estimates', {}).get(code, {})
            history[code] = {start: pair for start, pair in prior.items()
                             if float(start) > cutoff}
        start = int(datetime.fromisoformat(row['valid_at']).timestamp())
        if start >= issued:
            history[code][str(start)] = [row['precip_mm'], issued]
    snapshot['hourly_estimates'] = history


feed = sys.argv[1]
snapshot = {'kma-aws': observations, 'weatherapi': forecasts}[feed]()
if not snapshot['records']:
    raise SystemExit('Empty snapshot')
snapshot['collected_at'] = datetime.now(UTC).isoformat()
if feed == 'weatherapi':
    retain_hourly_estimates(snapshot)
output = Path('output')
output.mkdir(exist_ok=True)
(output / (feed + '.json')).write_text(json.dumps(snapshot, separators=(',', ':'), allow_nan=False))
print('Validated records:', len(snapshot['records']))
