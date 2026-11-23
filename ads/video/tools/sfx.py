#!/usr/bin/env python3
"""Synthesise the sound-effect track of a page from its cue list.

Usage: python3 -I ads/video/tools/sfx.py cues.json out.wav

cues.json = output of cues.js: {duration, cues:[{t, type, dur?}]}.
Sounds are synthesised by ffmpeg (aevalsrc / anoisesrc + filters); numpy only shapes the
envelope, normalises the peak and places each sound at its cue time.
  whoosh : band-passed noise, envelope over dur (0.2..0.4 s), peak -12 dBFS
  tick   : 2..4 kHz sine, 20..40 ms, peak -18 dBFS
  hit    : sine 120 -> 45 Hz, 0.3 s + short noise ring, peak -8 dBFS
  riser  : noise + sine 200 -> 3000 Hz over dur, growing, peak -14 dBFS
  subdrop: sine 60 -> 30 Hz, 0.6 s, peak -8 dBFS
gap / breath_on / breath_off / free make no sound (mix.py reads them).
Output: 48 kHz stereo PCM, exactly `duration` long.
"""
import json
import subprocess
import sys

import numpy as np

SR = 48000


def ff(src, filt=None, d=1.0):
    cmd = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', src]
    if filt:
        cmd += ['-af', filt]
    cmd += ['-t', f'{d:.4f}', '-ac', '1', '-ar', str(SR), '-f', 'f32le', '-']
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    n = int(round(d * SR))
    return np.pad(x, (0, max(0, n - len(x))))[:n]


def norm(x, peak_db):
    m = np.max(np.abs(x)) or 1.0
    return x / m * 10 ** (peak_db / 20)


def sweep(f0, f1, D):
    """aevalsrc expression of an exponential sine sweep f0 -> f1 over D seconds."""
    r = f1 / f0
    return f'sin(2*PI*{f0}*{D}/log({r})*(pow({r},t/{D})-1))'


def whoosh(d, seed):
    d = float(np.clip(d, 0.2, 0.4))
    x = ff(f'anoisesrc=color=pink:seed={seed}:sample_rate={SR}:amplitude=1',
           'highpass=f=350,bandpass=f=1400:width_type=h:w=2200,lowpass=f=6000', d)
    u = np.linspace(0, 1, len(x))
    env = np.where(u < .62, np.sin(np.pi / 2 * u / .62) ** 2, np.clip(np.cos(np.pi / 2 * (u - .62) / .38), 0, 1) ** 1.5)
    return norm(x * env, -12)


def tick(seed):
    f = 2000 + (seed * 733) % 2000
    d = 0.03
    x = ff(f'aevalsrc=\'sin(2*PI*{f}*t)*exp(-t*140)\':s={SR}', None, d)
    x[:48] *= np.linspace(0, 1, 48)
    return norm(x, -18)


def hit(seed):
    d = 0.3
    bass = ff(f'aevalsrc=\'{sweep(120, 45, d)}*exp(-t*9)\':s={SR}', None, d)
    ring = ff(f'anoisesrc=color=white:seed={seed}:sample_rate={SR}:amplitude=1', 'highpass=f=2500,bandpass=f=5200:width_type=h:w=3000', d)
    ring *= np.exp(-np.arange(len(ring)) / SR * 28)
    ping = ff(f'aevalsrc=\'sin(2*PI*1760*t)*exp(-t*18)\':s={SR}', None, d)
    x = bass + .35 * ring + .12 * ping
    x[:96] *= np.linspace(0, 1, 96)
    x[-480:] *= np.linspace(1, 0, 480)
    return norm(x, -8)


def riser(d, seed):
    d = max(0.3, float(d))
    nz = ff(f'anoisesrc=color=white:seed={seed}:sample_rate={SR}:amplitude=1', 'highpass=f=800,lowpass=f=9000', d)
    tone = ff(f'aevalsrc=\'{sweep(200, 3000, d)}\':s={SR}', None, d)
    u = np.linspace(0, 1, len(nz))
    env = u ** 2.2
    x = (.6 * nz + .5 * tone) * env
    x[-240:] *= np.linspace(1, 0, 240)
    return norm(x, -14)


def subdrop():
    d = 0.6
    x = ff(f'aevalsrc=\'{sweep(60, 30, d)}*(1-t/{d})\':s={SR}', None, d)
    x[:96] *= np.linspace(0, 1, 96)
    return norm(x, -8)


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    C = json.load(open(sys.argv[1], encoding='utf-8'))
    dur = float(C['duration'])
    out = np.zeros((int(round(dur * SR)), 2))
    counts = {}
    for i, c in enumerate(C['cues']):
        typ, t = c['type'], float(c['t'])
        if typ == 'whoosh':
            s = whoosh(c.get('dur', .3), 11 + i)
            pan = .35 * np.sin(i * 1.7)
        elif typ == 'tick':
            s, pan = tick(i), .25 * np.sin(i * 2.3)
        elif typ == 'hit':
            s, pan = hit(31 + i), 0
        elif typ == 'riser':
            s, pan = riser(c.get('dur', 1.0), 51 + i), 0
            t = t   # a riser cue marks its start; it grows for dur
        elif typ == 'subdrop':
            s, pan = subdrop(), 0
        else:
            continue
        counts[typ] = counts.get(typ, 0) + 1
        a = int(round(t * SR))
        if a >= len(out):
            continue
        b = min(len(out), a + len(s))
        seg = s[:b - a] if a >= 0 else s[-a:b - a]
        a = max(a, 0)
        out[a:a + len(seg), 0] += seg * np.sqrt(.5 - pan / 2) * np.sqrt(2)
        out[a:a + len(seg), 1] += seg * np.sqrt(.5 + pan / 2) * np.sqrt(2)
    pk = np.max(np.abs(out)) if len(out) else 0
    if pk > .98:   # overlapping sounds: keep clear of clipping
        out *= .98 / pk
    pcm = (np.clip(out, -1, 1) * 32767).astype('<i2').tobytes()
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 's16le', '-ar', str(SR), '-ac', '2', '-i', '-', sys.argv[2]],
                   input=pcm, check=True)
    print(json.dumps({'out': sys.argv[2], 'duration': dur, 'events': counts,
                      'peak_dbfs': round(20 * np.log10(max(pk, 1e-9)), 2)}), file=sys.stderr)


if __name__ == '__main__':
    main()
