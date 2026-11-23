#!/usr/bin/env python3
"""Place voice-over lines on the beat grid.

Usage: python3 -I ads/video/tools/vo_timeline.py <beats.json|beats.js> <first_beat> <gap> f1.wav f2.wav ... > vo.json

Line 1 starts on BB[first_beat]; every next line starts on the first beat that is at least
`gap` seconds after the end of the previous line. Durations come from ffprobe (trim silence first).
Prints [{file, t0, t1, dur, beat, delay_ms}] where delay_ms is how long the line waits after
(previous end + gap) for its beat. Only numpy-free stdlib + ffprobe.
"""
import json
import re
import subprocess
import sys


def load_beats(path):
    s = open(path, encoding='utf-8').read()
    s = re.sub(r'^\s*window\.BEATS\s*=\s*', '', s.strip()).rstrip(';')
    return json.loads(s)


def dur(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def main():
    a = sys.argv[1:]
    if len(a) < 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    B = load_beats(a[0])
    bb, bp = B['beats'], B['beat']
    first, gap, files = int(a[1]), float(a[2]), a[3:]

    def TB(k):   # beat index -> time, extrapolated past the grid
        n = len(bb) - 1
        return bb[n] + (k - n) * bp if k > n else bb[k]
    out, k, prev_end = [], first, None
    for f in files:
        if prev_end is not None:
            k = max(k + 1, 0)
            while TB(k) < prev_end + gap - 1e-9:
                k += 1
        t0 = TB(k)
        d = dur(f)
        delay = 0.0 if prev_end is None else (t0 - (prev_end + gap)) * 1000
        out.append({'file': f, 't0': round(t0, 4), 't1': round(t0 + d, 4), 'dur': round(d, 4), 'beat': k,
                    'delay_ms': round(delay, 1)})
        prev_end = t0 + d
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
