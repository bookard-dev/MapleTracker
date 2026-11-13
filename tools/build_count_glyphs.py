"""Builds count_glyphs.npz: the quick-slot count digits, in the game's own pixels.

Examples come from labelled recordings of the quick slots (captures/<rec>/*.png, 287x212 crops at
client x 1745, y 1100 of a 2560x1440 game, labels in captures/<rec>_labels.json) and from
labelled full screenshots (captures/extra_labels.json). Each count corner is turned back into
the game's 1366x768 pixels, where every digit is the same crisp shape wherever it sits. Where
each digit starts and how wide each digit is are learned together (guess, cut the examples out,
average them, find each digit again with the averages, re-measure, repeat). A template keeps
only the pixels that are the same in every example of that digit.

Run from the MapleTracker folder:
    python tools/build_count_glyphs.py            build count_glyphs.npz from everything
    python tools/build_count_glyphs.py --check    read every labelled count with it
    python tools/build_count_glyphs.py --holdout  build from the first recording's first 210 s only, then read
                                                  everything else cold and replay it through
                                                  PotionCounter like the app does
"""
import glob
import json
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import maple_tracker as mt  # noqa: E402

RECT = (0, 0, 2560, 1440)
S = 1440 / 768
RECORDINGS = {"qs": (1745, 1100), "qs2": (1745, 1100)}      # folder -> where the crop sits
SLOTS = [(0, 2), (1, 2), (0, 0)]                            # label order in the recordings
MAXW = 9
R = mt.CountReader


def corner(img, origin, slot):
    nx0, ny0, sx, sy = R.corner(slot, RECT, mt.DEFAULTS, S)
    return R.native(img, origin[0], origin[1], nx0, ny0, sx, sy)


def examples():
    """-> [(name, recording, time, slot, game-pixel corner, true count / None / "hidden")]"""
    out = []
    for rec, origin in RECORDINGS.items():
        path = os.path.join(HERE, "captures", rec + "_labels.json")
        if not os.path.exists(path):
            continue
        labels = json.load(open(path))
        for f in sorted(glob.glob(os.path.join(HERE, "captures", rec, "*.png"))):
            lab = labels.get(os.path.basename(f)[:-4])
            if lab is None:
                continue
            im = cv2.imread(f)
            for i, slot in enumerate(SLOTS):
                want = lab[i] if isinstance(lab, list) else "hidden"
                out.append((f"{rec}/{os.path.basename(f)}", rec, float(os.path.basename(f)[:-4]), slot,
                            corner(im, origin, slot), want))
    extra = os.path.join(HERE, "captures", "extra_labels.json")
    if os.path.exists(extra):
        for fname, slots in json.load(open(extra)).items():
            if fname.startswith("_"):
                continue
            im = cv2.imread(os.path.join(HERE, "captures", fname))
            for key, want in slots.items():
                slot = tuple(int(v) for v in key.split(","))
                out.append((fname, "extra", 0.0, slot, corner(im, (0, 0), slot), want))
    return out


def reliable(st):
    """Pixels to trust in a digit's template: the same in every example, and clearly the digit's
    ink - dark outline or bright fill - rather than whatever was behind it."""
    mean = st.mean(axis=0)
    return (st.std(axis=0) < 0.12) & ((mean < 0.3) | (mean > 0.55))


def cut(g, x, w):
    cell = g[:, max(0, x):max(0, x) + w]
    return np.pad(cell, ((0, 0), (0, w - cell.shape[1])), constant_values=0.5)


def build(data, out_path, quiet=False):
    # one example per distinct (recording, slot, count): forty identical frames of the same "4"
    # on the same icon would otherwise teach the icon as part of the 4
    seen, uniq = set(), []
    for name, rec, t, slot, g, v in data:
        if isinstance(v, int) and v > 0 and (rec, slot, v) not in seen:
            seen.add((rec, slot, v))
            uniq.append((g, str(v)))
    data = uniq
    adv = {d: 8 for d in "0123456789"}   # measured: digits start at 2 and step 8 ("1": 5)
    adv["1"] = 5
    start = 2
    pos = None
    tpl = {}
    for it in range(10):
        if pos is None:
            pos = []
            for g, text in data:
                xs, x = [], start
                for ch in text:
                    xs.append(x)
                    x += adv[ch]
                pos.append(xs)
        tpl = {}
        for ch in "0123456789":
            cs = [cut(g, x, adv[ch]) for (g, text), xs in zip(data, pos) for c, x in zip(text, xs) if c == ch]
            if cs:
                st = np.stack(cs)
                tpl[ch] = (st.mean(axis=0), reliable(st))
        new_pos, gaps = [], {}
        for g, text in data:
            xs, x = [], start
            for ch in text:
                tp, cf = tpl[ch]
                best = x                 # the game draws digits at exact places on its grid
                xs.append(best)
                x = best + adv[ch]
            for a, b, ch in zip(xs, xs[1:], text):
                gaps.setdefault(ch, []).append(b - a)
            new_pos.append(xs)
        moved = sum(a != b for p1, p2 in zip(pos, new_pos) for a, b in zip(p1, p2))
        pos = new_pos
        for ch, g_ in gaps.items():
            adv[ch] = int(round(np.median(g_)))
        start = int(round(np.median([xs[0] for xs in pos])))
        if not quiet:
            print(f"round {it}: {moved} moved; start {start}; widths {dict(sorted(adv.items()))}")
        if moved == 0 and it > 1:
            break
    labels, tpls, confs, widths = [], [], [], []
    for ch in sorted(tpl):
        w = adv[ch]
        cs = [cut(g, x, w) for (g, text), xs in zip(data, pos) for c, x in zip(text, xs) if c == ch]
        st = np.stack(cs)
        mean, conf = st.mean(axis=0), reliable(st)
        pad = lambda a, v: np.pad(a, ((0, 0), (0, MAXW - a.shape[1])), constant_values=v)
        labels.append(ch)
        tpls.append(pad(mean, 0.5))
        confs.append(pad(conf, False))
        widths.append(w)
        if not quiet:
            print(f"{ch}: {len(cs)} examples, width {w}, {int(conf.sum())} reliable pixels")
    missing = sorted(set("0123456789") - set(tpl))
    if missing:
        print("no examples yet of:", " ".join(missing))
    np.savez_compressed(out_path, native=np.array(1), labels=np.array(labels), start=np.array(start),
                        templates=np.array(tpls, np.float32), confident=np.array(confs), widths=np.array(widths))


def check(reader, data, show=True):
    ok = bad = unread = 0
    for name, rec, t, slot, g, want in data:
        got = reader.read(g)
        if want == "hidden":
            # something scrolled over the slots; reading the real count through it is fine too
            want_ok = got is None or got in (4, 125, 50)
        else:
            want_ok = got == want
        if want_ok:
            ok += 1
        elif got is None:
            unread += 1
            if show:
                print(f"  {name} {slot}: unread (truly {want})")
        else:
            bad += 1
            if show:
                print(f"  {name} {slot}: READ {got}, truly {want}")
    print(f"correct {ok}, unread {unread}, WRONG {bad}")


def replay(reader, data):
    """Every recording in order, each picture read 4 times (the app sees it many times)."""
    for rec in RECORDINGS:
        for slot in SLOTS:
            seq = sorted(((t, g, want) for name, r, t, sl, g, want in data if r == rec and sl == slot),
                         key=lambda e: e[0])
            truth = [w for _, _, w in seq if isinstance(w, int)]
            if len(truth) < 2:
                continue
            true_used = sum(max(0, a - b) for a, b in zip(truth, truth[1:]))
            ctr, used = mt.PotionCounter(), 0
            for t, g, want in seq:
                for _ in range(4):
                    used += ctr.update(reader.read(g, near=ctr.last), t)[0]
            print(f"replay {rec} slot {slot}: counted {used} used, truly {true_used}")


def main():
    data = examples()
    out = os.path.join(HERE, "count_glyphs.npz")
    if "--holdout" in sys.argv:
        train = [e for e in data if e[1] == "qs" and e[2] < 210]
        tmp = out + ".holdout.npz"
        build(train, tmp, quiet=True)
        r = mt.CountReader(tmp)
        os.remove(tmp)
        print("built from the first recording's first 210 s only; reading everything else:")
        check(r, [e for e in data if not (e[1] == "qs" and e[2] < 210)])
        replay(r, data)
        return
    if "--check" in sys.argv:
        r = mt.CountReader(out)
        check(r, data)
        replay(r, data)
        return
    build(data, out)
    print("saved", out)


if __name__ == "__main__":
    main()
