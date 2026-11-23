#!/usr/bin/env python3
"""Beat grid of a music track.

Usage: python3 -I ads/video/tools/beats.py <audio> [start] [dur] > beats.json

Prints JSON {bpm, beat, offset, beats[], downbeats[], hits[], energy_db_250ms[]}.
All times are seconds from `start` (so with start = M0 they are video times).

Method: log-spectral flux -> autocorrelation of the onset envelope in 70..180 BPM ->
fine period / phase search with a comb over the whole excerpt. The phase is then checked
against phase + 1/2 period on the band below 150 Hz (kick), otherwise the grid locks to hats
and sits ~200 ms off. Downbeat = the one of 4 bar phases with the most energy below 150 Hz.
hits = flux peaks above median + 3*MAD, at most one per 90 ms.
Only numpy + ffmpeg.
"""
import json
import subprocess
import sys

import numpy as np

SR = 22050
NFFT = 1024
HOP = 128


def load(path, start=0.0, dur=None):
    cmd = ['ffmpeg', '-v', 'error', '-ss', str(start), '-i', path]
    if dur is not None:
        cmd += ['-t', str(dur)]
    cmd += ['-ac', '1', '-ar', str(SR), '-f', 'f32le', '-']
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def stft_mag(x):
    pad = NFFT // 2
    x = np.concatenate([np.zeros(pad), x, np.zeros(pad)])
    n = 1 + (len(x) - NFFT) // HOP
    if n < 4:
        raise SystemExit('audio too short')
    win = np.hanning(NFFT)
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    return np.abs(np.fft.rfft(x[idx] * win, axis=1))   # frame i is centred at i*HOP/SR


def interp(env, t):
    """onset envelope sampled at times t (s), linear interpolation."""
    f = np.asarray(t) * SR / HOP
    i = np.clip(np.floor(f).astype(int), 0, len(env) - 2)
    k = np.clip(f - i, 0, 1)
    v = env[i] * (1 - k) + env[i + 1] * k
    v[(f < 0) | (f > len(env) - 1)] = 0
    return v


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    path = a[0]
    start = float(a[1]) if len(a) > 1 else 0.0
    dur = float(a[2]) if len(a) > 2 else None
    x = load(path, start, dur)
    T = len(x) / SR
    M = stft_mag(x)
    freqs = np.fft.rfftfreq(NFFT, 1 / SR)
    L = np.log1p(1000 * M)
    flux = np.maximum(0, np.diff(L, axis=0, prepend=L[:1])).sum(axis=1)
    low = freqs < 150
    lflux = np.maximum(0, np.diff(L[:, low], axis=0, prepend=L[:1, low])).sum(axis=1)
    # onset envelope: flux minus a moving local mean, half-wave rectified
    k = int(0.4 * SR / HOP) | 1
    ker = np.ones(k) / k
    env = np.maximum(0, flux - np.convolve(flux, ker, mode='same'))
    lenv = np.maximum(0, lflux - np.convolve(lflux, ker, mode='same'))
    # flux at frame i measures the change between frames i-1 and i: the onset lies half a hop earlier
    lat = 0.5 * HOP / SR

    # 1) tempo: autocorrelation in 70..180 BPM, with a mild preference for 100..140
    e = env - env.mean()
    ac = np.correlate(e, e, mode='full')[len(e) - 1:]
    fps = SR / HOP
    lags = np.arange(int(fps * 60 / 180), int(fps * 60 / 70) + 1)
    lags = lags[lags < len(ac)]
    bpms = 60 * fps / lags
    w = np.exp(-0.5 * (np.log2(bpms / 120) / 0.6) ** 2)
    score = ac[lags] * w
    lag0 = lags[np.argmax(score)]
    p0 = lag0 / fps

    # 2) fine period + phase: maximise the comb sum over the excerpt
    def comb(per, ph, en):
        ts = np.arange(ph, T, per)
        return interp(en, ts).sum() / max(1, len(ts))
    best = (-1, p0, 0)
    for per in np.linspace(p0 * 0.98, p0 * 1.02, 161):
        for ph in np.arange(0, per, 0.002):
            s = comb(per, ph, env)
            if s > best[0]:
                best = (s, per, ph)
    _, per, ph = best
    # sub-ms refinement
    for per2 in np.linspace(per - 0.0005, per + 0.0005, 41):
        for ph2 in np.arange(ph - 0.003, ph + 0.003, 0.00025):
            s = comb(per2, ph2, env)
            if s > best[0]:
                best = (s, per2, ph2)
    _, per, ph = best
    # 3) phase vs phase + half period, decided on the kick band (< 150 Hz, linear power:
    #    log flux there also fires on broadband hats)
    lowP = (M[:, low] ** 2).sum(axis=1)
    lowP = np.convolve(lowP, np.ones(9) / 9, mode='same')
    lowO = np.maximum(0, np.diff(lowP, prepend=lowP[:1]))
    half = (ph + per / 2) % per
    def kick(p):
        return comb(per, p, lowO) + comb(per, p + 0.01, lowO) + comb(per, p + 0.02, lowO)
    if kick(half) > 1.15 * kick(ph):
        ph = half
    # 3b) the STFT window smears onsets ~NFFT/2 early: shift the whole grid by the median offset
    #     between grid points and the sharpest rise of a 1 ms RMS envelope within +-50 ms
    ms = SR // 1000
    nn = len(x) // ms
    hr = np.sqrt(np.convolve((x[:nn * ms] ** 2).reshape(nn, ms).mean(axis=1), np.ones(4) / 4, mode='same'))
    dhr = np.diff(np.log(hr + 1e-4), prepend=np.log(hr[0] + 1e-4))
    offs = []
    for b in np.arange(ph, T, per):
        i0, i1 = int((b - 0.05) * SR / ms), int((b + 0.05) * SR / ms)
        if i0 < 1 or i1 >= nn:
            continue
        offs.append((i0 + int(np.argmax(dhr[i0:i1]))) * ms / SR - b)
    if offs:
        mo = float(np.median(offs))
        if abs(mo) < 0.05:
            ph += mo
    ph = ph % per
    beats = np.arange(ph, T + 1e-9, per)
    # 4) downbeat: bar phase with the most low-band energy
    lowE = lowP
    bar_scores = []
    for q in range(4):
        bq = beats[q::4]
        bar_scores.append(interp(lowE, bq + 0.02).sum() / max(1, len(bq)))
    q0 = int(np.argmax(bar_scores))
    downbeats = beats[q0::4]
    # 5) hits: flux peaks above median + 3 MAD, >= 90 ms apart
    med = np.median(flux)
    mad = np.median(np.abs(flux - med)) + 1e-9
    thr = med + 3 * mad
    hits, last = [], -1e9
    for i in range(1, len(flux) - 1):
        if flux[i] > thr and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1]:
            t = i * HOP / SR - lat
            if t - last >= 0.09:
                hits.append(t)
                last = t
    # 6) energy every 250 ms (dBFS RMS)
    n250 = int(0.25 * SR)
    energy = []
    for i in range(0, len(x), n250):
        seg = x[i:i + n250]
        energy.append(round(float(10 * np.log10(np.mean(seg ** 2) + 1e-12)), 1))
    out = {
        'bpm': round(60 / per, 3), 'beat': round(per, 5), 'offset': round(float(ph), 4),
        'start': start, 'duration': round(T, 3),
        'beats': [round(float(b), 4) for b in beats],
        'downbeats': [round(float(b), 4) for b in downbeats],
        'hits': [round(float(h), 4) for h in hits],
        'energy_db_250ms': energy,
    }
    print(json.dumps(out))


if __name__ == '__main__':
    main()
