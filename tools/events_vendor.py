#!/usr/bin/env python3
"""Copy flarepress's telemetry events files into an app, or check the app's copies.

    events_vendor.py [--client DIR] [--worker DIR] [--standalone DIR] [--check]

  --client DIR      events-client.js, the browser script (the app's web folder)
  --worker DIR      events-worker.js, handleEvents for the app's own Worker
  --standalone DIR  events-standalone.js and the events-worker.js it imports, for an app with no site
  --check           write nothing: exit 1 when a copy is missing, was edited, or is not flarepress's current file

Each copy starts with three comment lines: where it came from, how to update it, and the sha256 of the file as
flarepress has it. So a copy edited in the app is caught without flarepress (the hash of the rest no longer matches
the header), and a copy behind flarepress is caught by --check. A copy is never edited in the app: change it here,
release flarepress, and vendor it again. Stdlib only, Python 3.8+.
"""
import argparse
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'events')
HEADER = (
    '// Vendored from pdxfixit/flarepress events/{name}. Do not edit it here.\n'
    '// Update or check it from a flarepress checkout: just events-vendor --{flag} <this folder> [--check]\n'
    '// sha256 of the original: {sha}\n'
)
SHA = re.compile(r'^// sha256 of the original: ([0-9a-f]{64})\n')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def vendored(name, flag):
    with open(os.path.join(SOURCE, name), 'rb') as f:
        data = f.read()
    return HEADER.format(name=name, flag=flag, sha=digest(data)).encode() + data


def state(path, want):
    """None when the copy at path is flarepress's current file, else what is wrong with it."""
    try:
        with open(path, 'rb') as f:
            have = f.read()
    except OSError:
        return 'missing'
    lines = have.split(b'\n', 3)
    m = SHA.match(lines[2].decode('utf-8', 'replace') + '\n') if len(lines) == 4 else None
    if not m:
        return 'not a vendored copy (no header)'
    if digest(lines[3]) != m.group(1):
        return 'edited in the app (its body no longer matches its header)'
    if have != want:
        return 'behind flarepress (vendor it again)'
    return None


def plan(args):
    """[(target path, bytes)] for the flags given."""
    out = []
    if args.client:
        out.append((os.path.join(args.client, 'events-client.js'), vendored('events-client.js', 'client')))
    if args.worker:
        out.append((os.path.join(args.worker, 'events-worker.js'), vendored('events-worker.js', 'worker')))
    if args.standalone:
        out.append((os.path.join(args.standalone, 'events-standalone.js'),
                    vendored('events-standalone.js', 'standalone')))
        if os.path.realpath(args.standalone) != os.path.realpath(args.worker or ''):
            out.append((os.path.join(args.standalone, 'events-worker.js'), vendored('events-worker.js', 'standalone')))
    return out


def main(argv):
    p = argparse.ArgumentParser(prog='events_vendor.py', description=__doc__.split('\n')[0])
    p.add_argument('--client')
    p.add_argument('--worker')
    p.add_argument('--standalone')
    p.add_argument('--check', action='store_true')
    args = p.parse_args(argv[1:])
    files = plan(args)
    if not files:
        p.error('name at least one of --client, --worker, --standalone')
    bad = 0
    for path, data in files:
        if args.check:
            why = state(path, data)
            if why:
                bad += 1
                print('❌ %s: %s' % (path, why))
            else:
                print('✅ %s' % path)
            continue
        folder = os.path.dirname(path) or '.'
        if not os.path.isdir(folder):
            print('❌ %s is not a folder' % folder)
            bad += 1
            continue
        with open(path, 'wb') as f:
            f.write(data)
        print('📦 %s' % path)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
