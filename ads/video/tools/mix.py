#!/usr/bin/env python3
"""Mix music / voice / SFX under a silent render and mux it (video is copied, not re-encoded).

Usage:
  python3 -I ads/video/tools/mix.py video.mp4 <music.mp3|none> M0 out.mp4 \
      [--vo vo.json] [--sfx sfx.wav] [--cues cues.json] [--nomusic]

- music: excerpt from M0 for the video length, fade-in 30 ms, fade-out 1 s to the end;
  cues: gap {t,dur} -> music at 0 in [t, t+dur]; breath_on..breath_off -> low-pass ~700 Hz.
- levels: no voice -> music to -14 LUFS (by the final loudnorm).
  voice -> music bed normalised to -19 LUFS before ducking, voice to -15 LUFS,
  sidechaincompress threshold=0.05 ratio=4 attack=20 release=350 under the lines.
- SFX are scaled so their peak sits ~6 dB under the music peak, join the mix before loudnorm
  and never feed the sidechain.
- --nomusic (or music = none): voice + SFX to -14 LUFS; without voice only SFX, ~-20 LUFS.
- final: two-pass loudnorm I=<target> TP=-1.5 LRA=11 linear=true -> alimiter level=0 ->
  aresample=48000 -> AAC 192k -cutoff 18000; prints an ebur128=peak=true check at the end.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe_dur(path):
    out = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path], check=True).stdout
    return float(out.strip())


def lufs(path):
    """integrated loudness and true peak of a file (ebur128)."""
    err = run(['ffmpeg', '-nostats', '-i', path, '-af', 'ebur128=peak=true', '-f', 'null', '-']).stderr
    i = re.findall(r'I:\s+(-?[\d.]+|-inf) LUFS', err)
    p = re.findall(r'Peak:\s+(-?[\d.]+|-inf) dBFS', err)
    lra = re.findall(r'LRA:\s+(-?[\d.]+) LU', err)
    f = lambda v: float(v) if v not in ('-inf',) else -99.0
    return (f(i[-1]) if i else -99.0, f(p[-1]) if p else -99.0, f(lra[-1]) if lra else 0.0)


def max_volume(path):
    err = run(['ffmpeg', '-nostats', '-i', path, '-af', 'volumedetect', '-f', 'null', '-']).stderr
    m = re.findall(r'max_volume:\s+(-?[\d.]+|-inf) dB', err)
    return float(m[-1]) if m and m[-1] != '-inf' else -99.0


def to_wav(args, out):
    r = run(['ffmpeg', '-v', 'error', '-y'] + args + ['-ar', '48000', '-ac', '2', '-c:a', 'pcm_f32le', out])
    if r.returncode:
        sys.exit('ffmpeg failed: ' + r.stderr[-2000:])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video'); ap.add_argument('music'); ap.add_argument('m0', type=float); ap.add_argument('out')
    ap.add_argument('--vo'); ap.add_argument('--sfx'); ap.add_argument('--cues'); ap.add_argument('--nomusic', action='store_true')
    a = ap.parse_args()
    D = probe_dur(a.video)
    cues = json.load(open(a.cues, encoding='utf-8'))['cues'] if a.cues else []
    vo = json.load(open(a.vo, encoding='utf-8')) if a.vo else []
    use_music = a.music.lower() != 'none' and not a.nomusic
    tmp = tempfile.mkdtemp(prefix='mix_')
    parts = []   # (wav, label)

    # ---- music bed ----
    music_peak = None
    if use_music:
        f = [f'afade=t=in:st=0:d=0.03', f'afade=t=out:st={max(0, D - 1):.3f}:d=1']
        for c in cues:
            if c['type'] == 'gap':
                t0, t1 = c['t'], c['t'] + c.get('dur', .12)
                f.append(f"volume=enable='between(t,{t0:.4f},{t1:.4f})':volume=0")
        on = [c['t'] for c in cues if c['type'] == 'breath_on']
        off = [c['t'] for c in cues if c['type'] == 'breath_off']
        for t0, t1 in zip(on, off):
            f.append(f"lowpass=f=700:enable='between(t,{t0:.4f},{t1:.4f})'")
        mus = to_wav(['-ss', f'{a.m0:.4f}', '-t', f'{D:.4f}', '-i', a.music, '-af', ','.join(f)], os.path.join(tmp, 'music.wav'))
        if vo:   # bed to -19 LUFS before ducking
            I, _, _ = lufs(mus)
            mus = to_wav(['-i', mus, '-af', f'volume={-19 - I:.2f}dB'], os.path.join(tmp, 'music19.wav'))
        music_peak = max_volume(mus)

    # ---- voice ----
    voice = None
    if vo:
        ins, fl = [], []
        for i, v in enumerate(vo):
            ins += ['-i', v['file']]
            ms = int(round(v['t0'] * 1000))
            fl.append(f'[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[v{i}]')
        fl.append(''.join(f'[v{i}]' for i in range(len(vo))) + f'amix=inputs={len(vo)}:normalize=0,apad,atrim=0:{D:.4f}[vo]')
        voice = to_wav(ins + ['-filter_complex', ';'.join(fl), '-map', '[vo]'], os.path.join(tmp, 'vo_raw.wav'))
        I, _, _ = lufs(voice)
        voice = to_wav(['-i', voice, '-af', f'volume={-15 - I:.2f}dB'], os.path.join(tmp, 'vo.wav'))

    # ---- duck the music under the voice (sidechain from the voice only) ----
    if use_music and voice:
        mus = to_wav(['-i', mus, '-i', voice, '-filter_complex',
                      '[1:a]asplit=1[sc];[0:a][sc]sidechaincompress=threshold=0.05:ratio=4:attack=20:release=350[m]', '-map', '[m]'],
                     os.path.join(tmp, 'music_duck.wav'))
    if use_music:
        parts.append(mus)
    if voice:
        parts.append(voice)

    # ---- SFX: ~6 dB under the music peak; added before loudnorm, never into the sidechain ----
    if a.sfx:
        sfx = a.sfx
        if music_peak is not None:
            sp = max_volume(sfx)
            sfx = to_wav(['-i', sfx, '-af', f'volume={music_peak - 6 - sp:.2f}dB,apad,atrim=0:{D:.4f}'], os.path.join(tmp, 'sfx.wav'))
        else:
            sfx = to_wav(['-i', sfx, '-af', f'apad,atrim=0:{D:.4f}'], os.path.join(tmp, 'sfx.wav'))
        parts.append(sfx)
    if not parts:
        sys.exit('nothing to mix')

    # ---- pre-loudnorm mix ----
    ins = sum([['-i', p] for p in parts], [])
    pre = os.path.join(tmp, 'pre.wav')
    if len(parts) > 1:
        to_wav(ins + ['-filter_complex', ''.join(f'[{i}:a]' for i in range(len(parts))) + f'amix=inputs={len(parts)}:normalize=0:duration=longest,atrim=0:{D:.4f}[x]', '-map', '[x]'], pre)
    else:
        to_wav(ins + ['-af', f'apad,atrim=0:{D:.4f}'], pre)

    target = -14.0 if (use_music or voice) else -20.0
    # ---- two-pass loudnorm ----
    ln = f'loudnorm=I={target}:TP=-1.5:LRA=11'

    def measure(path):
        e = run(['ffmpeg', '-nostats', '-i', path, '-af', ln + ':print_format=json', '-f', 'null', '-']).stderr
        return json.loads(e[e.rindex('{'):e.rindex('}') + 1])
    m = measure(pre)
    # linear loudnorm only works if the needed gain does not push the true peak over -1.5 dBTP;
    # otherwise it silently switches to dynamic mode (pumping, misses the target). Pre-limit the peaks.
    for it in range(3):
        gain = target - float(m['input_i'])
        if float(m['input_tp']) + gain <= -2.0:
            break
        ceil = float(m['input_i']) + 11.0   # cap the peak-to-loudness ratio at ~11 dB (12.5 dB is the most -14 LUFS / -1.5 dBTP allows)
        lim = os.path.join(tmp, f'pre_lim{it}.wav')
        to_wav(['-i', pre, '-af', f'alimiter=limit={min(1.0, max(0.0625, 10 ** (ceil / 20))):.5f}:level=0:attack=2:release=60,aresample=48000'], lim)
        pre = lim
        m = measure(pre)
    ln2 = (f"{ln}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
           f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true:print_format=json")
    chain = f'{ln2},alimiter=limit={10 ** (-1.8 / 20):.4f}:level=0,aresample=48000'
    r = run(['ffmpeg', '-v', 'info', '-nostats', '-y', '-i', a.video, '-i', pre, '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
             '-af', chain, '-c:a', 'aac', '-b:a', '192k', '-cutoff', '18000', '-ar', '48000', '-ac', '2', '-shortest', '-movflags', '+faststart', a.out])
    if r.returncode:
        sys.exit('mux failed: ' + r.stderr[-3000:])
    try:
        m2 = json.loads(r.stderr[r.stderr.rindex('{'):r.stderr.rindex('}') + 1])
        mode = m2.get('normalization_type', '?')
    except ValueError:
        mode = '?'
    I, P, LRA = lufs(a.out)
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps({'out': a.out, 'target_lufs': target, 'music': use_music, 'voice': bool(voice), 'sfx': bool(a.sfx),
                      'loudnorm_pass1': {k: m[k] for k in ('input_i', 'input_tp', 'input_lra')}, 'loudnorm_mode': mode,
                      'ebur128': {'I': I, 'true_peak_dbfs': P, 'LRA': LRA}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
