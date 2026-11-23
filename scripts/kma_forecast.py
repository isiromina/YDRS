"""Publish only anonymous point IDs and KMA hourly PCP bounds. Times are interval ends."""
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

KST = timezone(timedelta(hours=9))


def grid(lat, lon):
    rad = math.radians
    a, b = rad(30), rad(60)
    t = lambda v: math.tan(math.pi / 4 + v / 2)
    n = math.log(math.cos(a) / math.cos(b)) / math.log(t(b) / t(a))
    f = math.cos(a) * t(a)**n / n * (6371.00877 / 5)
    r = f / t(rad(lat))**n
    theta = rad(lon - 126) * n
    return (math.floor(r * math.sin(theta) + 43.5),
            math.floor(f / t(rad(38))**n - r * math.cos(theta) + 136.5))


def bounds(value):
    s = str(value).replace('mm', '').replace(' ', '').strip()
    if s == '강수없음':
        return [0, 0]
    if re.fullmatch(r'\d+(\.\d+)?', s):
        return [float(s), float(s)]
    m = re.fullmatch(r'(\d+(?:\.\d+)?)(미만|이상)', s)
    if m:
        return [0, float(m[1])] if m[2] == '미만' else [float(m[1]), None]
    m = re.fullmatch(r'(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)', s)
    if m and float(m[1]) < float(m[2]):
        return [float(m[1]), float(m[2])]
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def retrieve(base, xy):
    args = dict(authKey=os.environ['KMA_KEY'].strip(), base_date=base.strftime('%Y%m%d'),
                base_time=base.strftime('%H00'), nx=xy[0], ny=xy[1],
                pageNo=1, numOfRows=1000, dataType='JSON')
    endpoint = 'https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst'
    try:
        with build_opener(NoRedirect()).open(Request(endpoint+'?'+urlencode(args)), timeout=35) as resp:
            payload = json.loads(resp.read(1500000))
    except HTTPError as exc:
        raise SystemExit(f'KMA forecast HTTP {exc.code}') from None
    except (URLError, TimeoutError, ValueError):
        raise SystemExit('KMA forecast unavailable or invalid response') from None
    response = payload.get('response', {})
    code = str(response.get('header', {}).get('resultCode', ''))
    if code != '00':
        raise SystemExit('KMA forecast result '+(code if code.isdigit() else 'unrecognized'))
    hours = {}
    for row in response['body']['items']['item']:
        if row.get('category') != 'PCP' or str(row.get('baseDate'))!=args['base_date'] or str(row.get('baseTime'))!=args['base_time']:
            continue
        stamp = datetime.strptime(row['fcstDate']+row['fcstTime'], '%Y%m%d%H%M').replace(tzinfo=KST)
        pair = bounds(row['fcstValue'])
        if pair is not None:
            hours[str(int(stamp.timestamp()))] = pair
    if not hours:
        raise SystemExit('No usable KMA hourly precipitation')
    return hours


def main():
    now = datetime.now(KST)
    base = (now - timedelta(minutes=20)).replace(minute=0, second=0, microsecond=0)
    base -= timedelta(hours=(base.hour - 2) % 3)
    points = json.loads(os.environ['WEATHER_POINTS'])
    cache, result = {}, {}
    for code, (lat, lon) in points.items():
        if not re.fullmatch(r'P\d{3,5}', code) or not (33 <= lat <= 39 and 124 <= lon <= 132):
            continue
        xy = grid(lat, lon)
        if xy not in cache:
            cache[xy] = retrieve(base, xy)
        result[code] = cache[xy]
    if not result:
        raise SystemExit('No domestic forecast points')
    path = Path('output/kma-forecast.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(source='KMA PCP', issued_at=base.isoformat(),
        collected_at=now.astimezone(timezone.utc).isoformat(), points=result), separators=(',', ':')))
    print('Validated domestic forecast points:', len(result))


if __name__ == '__main__':
    main()
