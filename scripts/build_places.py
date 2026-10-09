#!/usr/bin/env python3
"""Build a nationwide, static Czech toilet/shower point index from OSM PBF.

Input: Geofabrik Czech Republic extract. Uses pyosmium. This intentionally runs at
build/deploy time, never in an end user's mobile browser. No data is fabricated.
"""
from __future__ import annotations
import datetime as dt
import json
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
    'hgv', 'hgv:access', 'parking:hgv', 'truck_parking', 'parking:truck',
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
        wanted_refs = set()
        class Ways(osmium.SimpleHandler):
            def way(self, w):
                if not len(w.tags): return
                tags = dict(w.tags)
                if not is_facility(tags): return
                refs = [n.ref for n in w.nodes]
                if not refs: return
                way_elements.append((int(w.id), select_tags(tags), refs))
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
                if not is_facility(tags): return
                lat, lon = float(n.location.lat), float(n.location.lon)
                items.append({'type': 'node', 'id': ident, 'lat': round(lat, 7), 'lon': round(lon, 7), 'tags': select_tags(tags)})
        print('Pass 2: extracting toilet/shower points and way coordinates...', flush=True)
        Nodes().apply_file(str(pbf), locations=False)
        for ident, tags, refs in way_elements:
            coords = [coordinate_refs[ref] for ref in refs if ref in coordinate_refs]
            if not coords: continue
            lat = sum(p[0] for p in coords) / len(coords)
            lon = sum(p[1] for p in coords) / len(coords)
            items.append({'type': 'way', 'id': ident, 'lat': round(lat, 7), 'lon': round(lon, 7), 'tags': tags})
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
