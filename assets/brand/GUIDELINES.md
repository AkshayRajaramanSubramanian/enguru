# enguru brand guidelines

## The mark
**Descent E**: an E whose arms narrow step by step, ending in a dot. The narrowing mirrors the interview (explain → what if → spot the flaw), and the dot is the real bug you find at the end. It is the only coloured element, because attention goes where a senior would look.

Construction (on a 256 grid): one 36-unit stroke for the stem and arms; arms 168 / 132 / 88 long; every end rounded with r = 18. The dot (r = 18.6, a ~3% optical overshoot) sits on the bottom arm's line and closes the E's full width.

## Colour
| Role | Name | HEX | RGB |
|---|---|---|---|
| Primary | Ink | `#16181D` | 22, 24, 29 |
| Accent (the dot only) | Ember | `#E8590C` | 232, 89, 12 |
| Background | White | `#FFFFFF` | 255, 255, 255 |

- Ember is for the dot and for small highlights only. Don't set the whole mark in Ember on white, because the dot is what carries the meaning.
- **On an Ember background, the dot must be white or ink**, never Ember. Otherwise it disappears and the E reads as an F.
- One-colour use: all ink (`enguru-symbol-black.svg`) or all white (`enguru-symbol-white.svg`).
- Contrast: white on Ink is 17.4:1. Ember on Ink is about 5:1 and Ember on white about 3.6:1, which is fine for a graphic but too low for body text.

## Files
| Use | File |
|---|---|
| Plugin icon / app tile | `enguru-icon.svg`, `png/enguru-icon-1024.png` (also `.claude-plugin/icon.png`) |
| Symbol, colour | `enguru-symbol.svg` |
| Symbol on dark | `enguru-symbol-reversed.svg` |
| One colour | `enguru-symbol-black.svg`, `enguru-symbol-white.svg` |
| Horizontal lockup | `enguru-horizontal.svg` (+ black / white / ink variants) |
| Stacked lockup | `enguru-stacked.svg` |
| Web icons | `web/` (favicon.ico/svg, PNG set, `site.webmanifest`, `head-snippet.html`) |
| Presentation board | `slides/slide-01…05.png` |

## Clear space and sizes
- Clear space: the width of one arm (36 units, about 1/7 of the symbol height) on every side.
- Minimum size: symbol 16 px; horizontal lockup 24 px tall.
- In the lockup, the symbol's cap height equals 176/120 of the wordmark's x-height. Never rescale the two parts separately.

## Wordmark
`enguru` is drawn as custom monoline lowercase: a 30-unit stroke, round terminals and a geometric bowl. It is constructed, not typeset, so no font licence is involved. The letters are stroke-based paths, with the `e` as filled outlines. Expand the strokes to outlines before sending files to print or for cutting (vinyl, embroidery).

## Don't
- Recolour the arms, or make the dot any colour other than Ember, white or ink.
- Move the dot, separate it from the E, or join it to the bottom arm.
- Add effects such as shadows, gradients or outlines, or rotate or stretch the mark.
- Put the colour mark on a busy or Ember background. Use the white or reversed variant instead.

## Note
A trademark search has not been done. Run one before a public launch.
