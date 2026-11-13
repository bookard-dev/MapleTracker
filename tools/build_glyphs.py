"""Builds statusbar_glyphs.npz (the status bar's digit sprites) from captures/bar/*.png.

The captures are 670x30 strips of the status bar at 2560x1440 (client x 930.., y 1378..), taken
while hunting so every digit shows up somewhere in HP / MP / EXP. Glyphs are clustered by shape and
the clusters were labelled by eye; LABELS maps cluster index -> character (others are ignored:
'HP'/'MP'/'EXP' labels, '/', glyphs cut by the crop edges).

Run from the MapleTracker folder:  python tools/build_glyphs.py
"""
import glob
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from maple_tracker import glyph_masks, segments, norm_glyph  # noqa: E402

LABELS = {26: "0", 30: "0", 32: "0", 31: "1", 34: "1", 3: "2", 13: "2", 41: "2",
          21: "3", 25: "3", 29: "3", 27: "4", 37: "4", 5: "5", 11: "5", 18: "5",
          7: "6", 15: "6", 20: "6", 4: "7", 43: "7", 35: "8", 16: "9", 24: "9", 39: "9",
          22: "%"}


def crop_mask(m):
    ys, xs = np.nonzero(m)
    return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1] if len(ys) else None


ROUND = "035689"


def round_digit(g):
    """0/3/5/6/8/9 differ only in a few strokes, so loose clustering mixes them up. Decide from
    where the ink is (g is 12 rows high): right side of row 4, left side of rows 4 and 7, and a
    middle bar in rows 5-6."""
    cols = np.nonzero(g.max(axis=0) > 0.3)[0]
    a, b = cols[0], cols[-1] + 1
    third = max(1, (b - a) // 3)
    ur = g[4, b - third:b].max() > 0.5
    ul = g[4, a:a + third].max() > 0.5
    ll = g[7, a:a + third].max() > 0.5
    mid = g[5:7, a + third:b - third].max() > 0.5
    if not ur:
        return "6" if ll else "5"
    if not mid:
        return "0"
    if ll:
        return "8"
    return "9" if ul else "3"


def main():
    clusters = []      # [8x12 key, (w, h), [normalised glyphs]]
    for f in sorted(glob.glob(os.path.join(HERE, "captures", "bar", "*.png"))):
        im = cv2.imread(f)
        for a, b in [(195, 330), (410, 520), (470, 660)]:
            white, green = glyph_masks(im[8:24, a:b])
            m = white | green
            for x0, x1 in segments(m):
                gm = crop_mask(m[:, x0:x1])
                if gm is None:
                    continue
                h, w = gm.shape
                key = cv2.resize(gm.astype(np.float32), (8, 12), interpolation=cv2.INTER_AREA)
                for cl in clusters:
                    if abs(cl[1][0] - w) <= 2 and abs(cl[1][1] - h) <= 2 and np.abs(cl[0] - key).mean() < 0.08:
                        cl[2].append(norm_glyph(gm))
                        break
                else:
                    clusters.append([key, (w, h), [norm_glyph(gm)]])
    tpl, lab = [], []
    
    by_label = {}
    for i, ch in LABELS.items():
        for v in clusters[i][2]:
            by_label.setdefault(round_digit(v) if ch in ROUND else ch, []).append(v)
    for ch, variants in sorted(by_label.items()):
        # a few distinct-looking examples per cluster are plenty
        picked = []
        for v in variants:
            if all(np.abs(v - p).mean() > 0.03 for p in picked):
                picked.append(v)
        tpl += picked[:8]
        lab += [ch] * len(picked[:8])
        print(f"{ch}: {len(variants)} seen, {len(picked[:8])} kept")
    if "--show" in sys.argv:
        for g, ch in zip(tpl, lab):
            print("--", ch)
            for row in g:
                print("   " + "".join("#" if p > 0.5 else ("+" if p > 0.2 else ".") for p in row))
    np.savez_compressed(os.path.join(HERE, "statusbar_glyphs.npz"),
                        templates=np.array(tpl, np.float32), labels=np.array(lab))
    print("saved", len(tpl), "templates")


if __name__ == "__main__":
    main()
