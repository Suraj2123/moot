/**
 * The moot wordmark.
 *
 * Drawn in CSS rather than shipped as an image. The logo is a single word in a
 * heavy weight with a stepped grey extrude behind it, which `text-shadow`
 * reproduces exactly -- and doing it this way means it inherits the app's own
 * face, scales to any size without a second asset, recolours with the theme,
 * and adds nothing to the bundle.
 *
 * The extrude is five offsets rather than one. A single shadow reads as a drop
 * shadow; a stack one pixel apart reads as depth, which is what the original
 * does.
 */
export function Wordmark({ size = 72 }: { size?: number }) {
  const step = Math.max(1, Math.round(size / 24));
  // Fewer layers when the mark is small. Five one-pixel offsets under a 19px
  // word is not depth, it is a smudge -- the extrude has to stay a fraction of
  // the letterform rather than a fixed stack.
  const layers = size >= 40 ? 5 : size >= 24 ? 3 : 2;
  const shadow = Array.from({ length: layers }, (_, i) => i + 1)
    .map((n) => `${n * step}px ${n * step}px 0 var(--wordmark-shadow)`)
    .join(", ");

  return (
    <span
      className="wordmark"
      style={{ fontSize: size, textShadow: shadow, paddingRight: step * layers }}
      aria-label="moot"
      role="img"
    >
      moot
    </span>
  );
}
