#!/usr/bin/env python3
"""Build a nationwide, static Czech toilet/shower point index from OSM PBF.

Input: Geofabrik Czech Republic extract. Uses pyosmium. This intentionally runs at
build/deploy time, never in an end user's mobile browser. No data is fabricated.
"""
from __future__ import annotations
import datetime as dt
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
import urllib.request

SOURCE = os.environ.get('PAUZA_OSM_SOURCE', 'https://download.geofabrik.de/europe/czech-republic-latest.osm.pbf')
OUT = Path(__file__).resolve().parents[1] / 'public' / 'data' / 'wc-cr.json'
ALLOWED = {
    'amenity', 'highway', 'toilets', 'shower', 'toilets:shower', 'fee', 'charge',
    'toilets:fee', 'toilets:charge', 'shower:fee', 'shower:charge',
    'wheelchair', 'toilets:wheelchair', 'opening_hours', 'toilets:opening_hours',
    'access', 'toilets:access', 'name', 'name:cs', 'operator', 'brand',
    'addr:street', 'addr:housenumber', 'addr:place', 'addr:city',
    'addr:postcode', 'addr:municipality', 'contact:street', 'contact:housenumber',
    'contact:city', 'contact:postcode', 'contact:place',
    'hgv', 'hgv:access', 'parking:hgv', 'truck_parking', 'parking:truck', 'parking',
    'toilets:changing_table', 'changing_table', 'drinking_water',
    'website', 'contact:website', 'phone', 'contact:phone', 'source', 'check_date',
}

def is_facility(t: dict) -> bool:
    kind = t.get('amenity')
    if kind in ('toilets', 'shower'):
        return True
    facility = kind in ('fuel', 'parking') or t.get('highway') in ('services', 'rest_area')
    return facility and (t.get('toilets') == 'yes' or t.get('shower') == 'yes' or t.get('toilets:shower') == 'yes')

def select_tags(tags):
    return {k: str(v) for k, v in tags.items() if k in ALLOWED}


def truck_context(t: dict) -> str | None:
    """Return an OSM-mapped candidate truck stop, not a verified entrance to a WC."""
    if str(t.get('hgv', '')).lower() in ('no', 'private') or str(t.get('hgv:access', '')).lower() == 'no':
        return None
    tagged = any(str(t.get(k, '')).lower() in ('yes', 'designated')
                 for k in ('hgv', 'hgv:access', 'parking:hgv', 'truck_parking', 'parking:truck'))
    if t.get('amenity') == 'parking' and tagged:
        return 'hgv_parking'
    if t.get('highway') in ('services', 'rest_area'):
        return 'rest_area'
    return None


def nearby_context(items: list, anchors: list):
    """Annotate toilets near mapped stopping places; proximity does not prove access."""
    # 0.01 degrees ~ 0.7-1.1 km in the Czech Republic: hash-grid lookup.
    cells = {}
    for lat, lon, kind in anchors:
        key=(math.floor(lat * 100), math.floor(lon * 100))
        cells.setdefault(key, []).append((lat, lon, kind))
    for p in items:
        t = p['tags']
        if truck_context(t):
            t['pauza:truck_context'] = truck_context(t)
            t['pauza:truck_distance_m'] = '0'
            continue
        lat, lon = p['lat'], p['lon']
        la, lo = math.floor(lat * 100), math.floor(lon * 100)
        best = None
        # 3x3 cells covers nearest candidates under 650m.
        for i in range(la - 1, la + 2):
            for j in range(lo - 1, lo + 2):
                for a_lat, a_lon, kind in cells.get((i, j), []):
                    dlat = (lat-a_lat) * 111_195
                    dlon = (lon-a_lon) * 111_195 * math.cos(math.radians(lat))
                    dist = math.hypot(dlat, dlon)
                    limit = 380 if kind == 'hgv_parking' else 600
                    if dist <= limit and (best is None or (kind == 'hgv_parking', -dist) > (best[0]=='hgv_parking', -best[1])):
                        best = (kind, dist)
        if best:
            t['pauza:truck_context'] = best[0]
            t['pauza:truck_distance_m'] = str(round(best[1]))

def download(url, path):
    last_exc = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'PAUZA data-builder/1.0 (OpenStreetMap ODbL; static extract)'})
            with urllib.request.urlopen(req, timeout=120) as src, open(path, 'wb') as out:
                while True:
                    block = src.read(4 * 1024 * 1024)
                    if not block: break
                    out.write(block)
            if os.path.getsize(path) < 1_000_000:
                raise RuntimeError('OSM extract unexpectedly small')
            return
        except Exception as exc:
            last_exc = exc
            print('Download attempt', attempt + 1, 'failed:', str(exc), flush=True)
            if attempt < 2: time.sleep(12 * (attempt + 1))
    raise RuntimeError('Failed to download valid OSM data; aborting deploy, leaving old site live') from last_exc

def run():
    import osmium
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='pauza-osm-') as td:
        pbf = Path(td) / 'czech-republic.osm.pbf'
        print('Downloading Czech Republic OSM data (large file, once per deployment)...', flush=True)
        download(SOURCE, pbf)
        print('Downloaded', pbf.stat().st_size, 'bytes', flush=True)
        way_elements = []
        truck_ways = []
        truck_points = []
        wanted_refs = set()
        class Ways(osmium.SimpleHandler):
            def way(self, w):
                if not len(w.tags): return
                tags = dict(w.tags)
                facility = is_facility(tags)
                context = truck_context(tags)
                if not facility and not context: return
                refs = [n.ref for n in w.nodes]
                if not refs: return
                if facility: way_elements.append((int(w.id), select_tags(tags), refs))
                if context: truck_ways.append((refs, context))
                wanted_refs.update(refs)
        print('Pass 1: collecting tagged ways...', flush=True)
        Ways().apply_file(str(pbf), locations=False)
        print(len(way_elements), 'facilities on ways,', len(wanted_refs), 'referenced nodes', flush=True)
        coordinate_refs = {}
        items = []
        cities = []
        class Nodes(osmium.SimpleHandler):
            def node(self, n):
                if not n.location.valid(): return
                ident = int(n.id)
                if ident in wanted_refs:
                    coordinate_refs[ident] = (round(float(n.location.lat), 7), round(float(n.location.lon), 7))
                if not len(n.tags): return
                tags = dict(n.tags)
                if tags.get('place') in {'city', 'town', 'village', 'suburb', 'quarter', 'neighbourhood', 'hamlet'} and tags.get('name'):
                    cities.append({'name': str(tags.get('name:cs') or tags['name']),
                                   'lat': round(float(n.location.lat), 7),
                                   'lon': round(float(n.location.lon), 7),
                                   'kind': tags['place']})
                lat, lon = float(n.location.lat), float(n.location.lon)
                context = truck_context(tags)
                if context: truck_points.append((lat, lon, context))
                if not is_facility(tags): return
                items.append({'type': 'node', 'id': ident, 'lat': round(lat, 7), 'lon': round(lon, 7), 'tags': select_tags(tags)})
        print('Pass 2: extracting toilet/shower points and way coordinates...', flush=True)
        Nodes().apply_file(str(pbf), locations=False)
        for ident, tags, refs in way_elements:
            coords = [coordinate_refs[ref] for ref in refs if ref in coordinate_refs]
            if not coords: continue
            lat = sum(p[0] for p in coords) / len(coords)
            lon = sum(p[1] for p in coords) / len(coords)
            items.append({'type': 'way', 'id': ident, 'lat': round(lat, 7), 'lon': round(lon, 7), 'tags': tags})
        for refs, context in truck_ways:
            coords = [coordinate_refs[ref] for ref in refs if ref in coordinate_refs]
            if not coords: continue
            truck_points.append((sum(v[0] for v in coords)/len(coords), sum(v[1] for v in coords)/len(coords), context))
        nearby_context(items, truck_points)
        context_found=sum(1 for p in items if p['tags'].get('pauza:truck_context'))
        print('Found', len(truck_points), 'truck parking/service-area contexts; annotated', context_found, 'WC/shower points', flush=True)
        items.sort(key=lambda e: (e['type'], e['id']))
        if len(items) < 500:
            raise RuntimeError(f'Only {len(items)} places extracted; refusing to publish an incomplete dataset')
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix('.json.tmp')
        stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'meta': {'generated_at': stamp, 'source': 'OpenStreetMap contributors via Geofabrik',
                                'license': 'ODbL 1.0', 'count': len(items), 'cities': cities, 'note': 'Node and way facilities; ways approximated by geometric center'},
                       'elements': items}, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, OUT)
        print(f'BUILD OK: {len(items)} WC / showers, {OUT.stat().st_size} bytes, {time.monotonic()-started:.1f} seconds', flush=True)

if __name__ == '__main__':
    run()
