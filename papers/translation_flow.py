"""Fit translated paragraphs at their source font size on the displayed page."""
from dataclasses import dataclass
from io import BytesIO
from math import floor

import fitz


@dataclass
class Paragraph:
    key: str
    source: fitz.Rect
    story: fitz.Story
    formulas: list
    line_height: float
    anchored: bool
    height: float = 0
    earliest: float = 0
    ceiling: float = 0
    capacity: float = 0
    top: float = 0

    def measure(self):
        more, filled = self.story.place(fitz.Rect(0, 0, self.source.width, 1_000_000))
        if more:
            raise ValueError("Paragraph exceeds PDF layout limits")
        return filled[3] + .05

    def fragment(self, height):
        """Draw only the consumed lines. Story resumes at the following line."""
        area = fitz.Rect(0, 0, self.source.width, height)
        more, _ = self.story.place(area)
        buffer = BytesIO()
        writer = fitz.DocumentWriter(buffer)
        self.story.draw(writer.begin_page(area))
        writer.end_page()
        writer.close()
        doc = fitz.open(stream=buffer.getvalue(), filetype="pdf")
        page = doc[0]
        slots = page.get_image_info()
        for slot, (image, original, ascent) in zip(slots, self.formulas[:len(slots)], strict=True):
            bounds = fitz.Rect(slot["bbox"])
            baseline = bounds.y1
            em = bounds.height * 2
            bottom = min(height, baseline + .32 * em)
            top = max(0, baseline + .32 * em - self.line_height)
            scale = min(bounds.width / original.width, (bottom-top) / original.height)
            w, h = original.width*scale, original.height*scale
            x = (bounds.x0+bounds.x1-w)/2
            y = max(top, min(baseline-ascent*scale, bottom-h))
            page.insert_image(fitz.Rect(x,y,x+w,y+h), stream=image)
        del self.formulas[:len(slots)]
        return doc, bool(more)


def _shares_width(a, b):
    return min(a.x1,b.x1) - max(a.x0,b.x0) > .5


def layout_paragraphs(plans, obstacles, view):
    # First find the earliest legal position, compacting gaps only when a
    # paragraph needs that space. Obstacles stay in their original coordinates.
    ordered = sorted(plans, key=lambda p: (p.source.y0,p.source.x0))
    flowing = []
    anchors = []
    for p in ordered:
        p.height = p.measure()
        if p.anchored and p.height > p.source.height+max(6, p.line_height/2):
            p.anchored = False
        if p.anchored:
            p.top = min(view.height-p.height,max(0,(p.source.y0+p.source.y1-p.height)/2))
            p.capacity = p.height
            anchors.append(fitz.Rect(p.source.x0,p.top,p.source.x1,p.top+p.height))
    for p in ordered:
        if p.anchored:
            continue
        r = p.source
        top, bottom = min(16,r.y0), max(view.height-16,r.y1)
        for block in (*obstacles, *anchors):
            if not _shares_width(r, block):
                continue
            if block.y1 <= r.y0+1:
                top = max(top,block.y1+1)
            elif block.y0 >= r.y1-1:
                bottom = min(bottom,block.y0-1)
            elif block.intersects(r):
                # Keep remaining original artwork intact if a source text
                # rectangle straddled it. Continue the text below the page.
                bottom = min(bottom,block.y0-1)
        earlier = [q for q in flowing if _shares_width(r,q.source)]
        p.earliest = max([top]+[q.earliest+q.capacity+2 for q in earlier])
        p.ceiling = bottom
        room = max(0, bottom-p.earliest)
        if any(q.capacity < q.height for q in earlier):
            p.capacity = 0
        elif p.height <= room:
            p.capacity = p.height
        else:
            p.capacity = max(0, floor((room-.05)/p.line_height)*p.line_height+.02) if room >= p.line_height+.05 else 0
        flowing.append(p)
    # Work back from fixed obstacles so short preceding translations can lend
    # whitespace to a later one without pulling every paragraph up the page.
    placed = []
    for p in reversed(flowing):
        if p.capacity:
            ceiling = min([p.ceiling]+[q.top-2 for q in placed if _shares_width(p.source,q.source)])
            p.top = max(p.earliest, min(p.source.y0,ceiling-p.capacity))
            placed.append(p)
    return ordered


def extend_page(page, extra):
    """Grow in displayed reading direction, keeping the old crop and annotations aligned."""
    media, crop = page.mediabox, page.cropbox
    original = fitz.Rect(crop.x0,media.y1-crop.y1,crop.x1,media.y1-crop.y0)
    box = fitz.Rect(original)
    if page.rotation == 90:
        box.x1 += extra
    elif page.rotation == 180:
        box.y1 += extra
    elif page.rotation == 270:
        box.x0 -= extra
    else:
        box.y0 -= extra
    # Expanding the crop box must not expose previously clipped publisher marks.
    stream = page.parent.get_new_xref()
    page.parent.update_object(stream, "<<>>")
    clip = f"q {original.x0} {original.y0} {original.width} {original.height} re W n\n"
    page.parent.update_stream(stream,clip.encode()+page.read_contents()+b"\nQ")
    page.set_contents(stream)
    page.set_mediabox(box)
