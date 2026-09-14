"""Activity photos, shaped for a public page.

Strava returns a `location` on every photo: the raw EXIF coordinate of where the
shutter was pressed, to full float precision. Probed on 2026-09-13, one photo on
a public gravel ride carried `[42.51111111111111, -76.55777777777777]`.

That coordinate is NOT privacy-zone trimmed, is not related to the activity's
trimmed polyline, and on any photo taken before setting off it IS the athlete's
front door. Nothing downstream would notice: it arrives as an ordinary field on
an ordinary photo record, and a page that only ever renders `urls` would carry
it in its committed JSON regardless.

So `location` is stripped here, at the point the record is built, rather than
being left to a caller to remember. `strip_photo` is the only constructor, and
`test_photos.py` asserts that a location present on the way in is absent on the
way out.
"""

#: Fields worth publishing. Everything else on a Strava photo record is either
#: internal bookkeeping or, in the case of `location`, actively unsafe.
_KEEP = ("unique_id", "activity_id", "activity_name", "caption", "created_at_local")

#: Never emitted, whatever a caller asks for.
FORBIDDEN_FIELDS = ("location",)


def strip_photo(photo: dict, size: int) -> dict | None:
    """Reduce one Strava photo record to what a public page may show.

    Returns None when the record carries no usable image URL at the requested
    size, which happens on placeholders and on photos still processing.
    """
    urls = photo.get("urls") or {}
    url = urls.get(str(size)) or urls.get(size)
    if not url:
        return None

    out = {k: photo.get(k) for k in _KEEP}
    out["url"] = url
    sizes = (photo.get("sizes") or {}).get(str(size)) or (photo.get("sizes") or {}).get(size)
    if isinstance(sizes, (list, tuple)) and len(sizes) == 2:
        out["width"], out["height"] = int(sizes[0]), int(sizes[1])

    # Belt and braces: the field is never copied above, and is asserted absent
    # here so a future edit to _KEEP cannot quietly reintroduce it.
    for field in FORBIDDEN_FIELDS:
        out.pop(field, None)
    return out


def pick_for_spot(photos: list[dict], per_spot: int = 1) -> list[dict]:
    """Choose the photos to publish for one spot.

    Prefers a captioned photo, because a caption is the athlete saying this one
    is worth looking at, and gives a page real alt text instead of a filename.
    Ties break towards the most recent.
    """
    ranked = sorted(
        photos,
        key=lambda p: (bool((p.get("caption") or "").strip()), p.get("created_at_local") or ""),
        reverse=True,
    )
    return ranked[:per_spot]
