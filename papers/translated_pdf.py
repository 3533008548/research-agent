"""Write a standalone, page-aligned Chinese PDF alongside its library source."""
from contextlib import ExitStack
import html
import json
import os
from papers.translation_layout import (INLINE_TOKEN, SECTION_HEADING, inline_layout, identifier_strokes,
                                      native_lines, lines_in, ruled_tables, typography)
from papers.translation_flow import Paragraph, extend_page, layout_paragraphs
import uuid


_BODY_LINE_HEIGHT_PT = 15

# A transparent pixel reserves each formula's width during wrapping. Its
# bottom edge sits on the same baseline as the surrounding text in MuPDF.
_SPACER = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAACXBIWXMAAA7EAAAOxAGVKw4bAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=")


def _content(kind, text):
    body = html.escape(text).replace("\n", "<br>")
    return "<b>" + body + "</b>" if kind == "heading" else body


def _page_translations(db, identifier, page):
    return db.execute("""SELECT s.id,s.source,s.translation,s.kind,s.bounds FROM translation_segments s
                         JOIN translation_jobs j ON j.document_id=s.document_id AND j.layout_ready=1
                         WHERE s.document_id=? AND s.page=? ORDER BY s.rowid""", (identifier, page)).fetchall()


def _place_translations(page, rows):
    if not rows:
        return
    import fitz

    plans = []
    anchors = []
    bullets = {}
    view = page.rect
    lines = native_lines(page)
    _, clips, _ = inline_layout(lines)
    drawings = page.get_drawings()
    protected_regions = ruled_tables(lines, drawings)
    protected_regions.extend(r for r in page.cluster_drawings() if r.width >= 24 and r.height >= 12
                             and r.get_area() < .85*view.get_area())
    strokes = [r for r, _, _ in identifier_strokes(page, drawings)]
    marks = [p["rect"] for p in drawings if p.get("fill") is not None and len(p["items"]) == 1
             and p["items"][0][0] == "re" and 1 <= p["rect"].width <= 8 and 1 <= p["rect"].height <= 8]
    # Preserve the source typography where the translation is identical,
    # including standalone equations and symbols inside diagrams.
    for row in rows:
        text = row["translation"]
        x,y,r,b = json.loads(row["bounds"])
        rect = fitz.Rect(x*view.width,y*view.height,r*view.width,b*view.height) * page.derotation_matrix
        if rect.is_empty:
            continue
        key = row["id"] if "id" in row.keys() else ""
        if text is None or text.strip() == row["source"].strip():
            anchors.append((key,rect * page.rotation_matrix))
            continue
        # Keep original raster images and vector lines while removing the replaced text.
        native = lines_in(rect, lines)
        if row["kind"] == "membership":
            role_spans = [span for line in lines for span in line["spans"]
                          if rect.contains((fitz.Rect(span["bbox"]).tl+fitz.Rect(span["bbox"]).br)/2)]
            if role_spans:
                native = [{"bbox":rect, "spans":role_spans}]
        size, bold, centered, indent = typography(rect, native)
        # Justified body lines can have near-identical centers. Alignment
        # requires title evidence as well as geometry.
        centered = ((row["kind"] == "heading" or size >= 15) and
                    (centered or abs((rect.x0+rect.x1-page.cropbox.width)/2) < .65*size))
        if SECTION_HEADING.fullmatch(row["source"].strip()) and native:
            # IEEE small caps are drawn as smaller capital glyphs. Use the
            # heading's leading capital and the center of its local text column.
            size = max(s["size"] for line in native for s in line["spans"])
            center = (rect.x0+rect.x1)/2
            centered |= any(abs((l["bbox"][0]+l["bbox"][2])/2-center) < size
                            and l["bbox"][2]-l["bbox"][0] > rect.width+size
                            for l in lines)
        fixed_height = None if row["kind"] in {"heading", "membership"} or centered else _BODY_LINE_HEIGHT_PT
        leading = "1.08" if fixed_height is None else f"{fixed_height}pt"
        css = (f"*{{margin:0;padding:0}} body{{margin:0;font-family:serif;font-size:{size}pt;line-height:{leading}}}"
               f"div{{text-align:{'center' if centered else 'left'};text-indent:{0 if centered else indent}pt;"
               f"font-weight:{'bold' if bold else 'normal'}}}")
        markup = '<div lang="zh-Hans">'+_content(row["kind"],text)+'</div>'
        formulas = []
        def reserve_formula(match):
            area = clips[html.unescape(match[0])]
            line = lines[int(match[1].split(".")[0])]
            base = max(line["spans"], key=lambda s: (round(s["size"]), len(s["text"].strip())))
            ascent = base["origin"][1] - area.y0
            pix = page.get_pixmap(matrix=page.derotation_matrix*fitz.Matrix(4,4), clip=area*page.rotation_matrix)
            formulas.append((pix.tobytes("png"), area, ascent))
            return f'<img style="width:{area.width / size}em;height:0.5em" src="{_SPACER}">'
        markup = INLINE_TOKEN.sub(reserve_formula, markup)
        displayed = rect * page.rotation_matrix
        if "-item" in key and native and row["kind"] == "text":
            first = fitz.Rect(native[0]["bbox"])
            mark = next((r for r in marks if 0 <= first.x0-r.x1 < 1.8*size
                         and first.y0 < (r.y0+r.y1)/2 < first.y1), None)
            if mark is not None:
                clip = (mark+(-.6,-.6,.6,.6)) * page.rotation_matrix
                relative = clip * fitz.Matrix(1,0,0,1,-displayed.x0,-displayed.y0)
                bullets[key] = (relative,page.get_pixmap(clip=clip,matrix=fitz.Matrix(3,3)))
                strokes.append(mark)
        anchored = (row["kind"] == "membership" or displayed.y1 < .055*view.height
                    or displayed.y0 > .955*view.height)
        plans.append(Paragraph(key, displayed, fitz.Story(markup, user_css=css), formulas,
                               fixed_height or 1.08*size, anchored))
    if plans:
        orcids = [link for link in page.get_links() if
                  link.get("uri","").lower().startswith(("https://orcid.org/", "http://orcid.org/"))]
        # Some publisher headers rely on clipped text. MuPDF redaction can
        # expose the clipped glyphs well outside the changed paragraphs.
        # Restore untouched margin strips only if their appearance changed.
        margins = [fitz.Rect(0, 0, view.width, view.height * .045),
                   fitz.Rect(0, view.height * .965, view.width, view.height)]
        zoom = min(3, 3000 / max(view.width, view.height))
        matrix = fitz.Matrix(zoom, zoom)
        margins = [(clip, page.get_pixmap(clip=clip, matrix=matrix)) for clip in margins
                   if not any(clip.intersects(p.source) for p in plans)]
        # Remove only confirmed identifier strokes inside replaced prose. A
        # graphics-wide redaction here would also erase fraction bars and grids.
        removed = False
        for stroke in strokes:
            if stroke in marks or any(p.source.contains(((stroke.tl+stroke.br)/2)*page.rotation_matrix) for p in plans):
                page.add_redact_annot(stroke + (-1,-1,1,1), fill=False)
                removed = True
        if removed:
            page.apply_redactions(images=0, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=fitz.PDF_REDACT_TEXT_NONE)
        for p in plans:
            page.add_redact_annot(p.source * page.derotation_matrix, fill=False)
        page.apply_redactions(images=0, graphics=0)
        surviving = {link.get("xref") for link in page.get_links()}
        for link in orcids:
            if link["xref"] not in surviving:
                page.insert_link({**link, "from":link["from"] * page.derotation_matrix})
        for clip, image in margins:
            if page.get_pixmap(clip=clip, matrix=matrix).digest != image.digest:
                page.insert_image(clip * page.derotation_matrix, pixmap=image, rotate=page.rotation,
                                  keep_proportion=False)
    # Only text and artwork still visible after replacement are barriers.
    remaining = protected_regions + [fitz.Rect(l["bbox"]) for l in native_lines(page)]
    remaining.extend(fitz.Rect(i["bbox"]) for i in page.get_image_info())
    remaining.extend(p["rect"]+(-.2,-.2,.2,.2) for p in page.get_drawings()
                     if p["rect"].get_area() < .85*view.get_area())
    obstacles = [r * page.rotation_matrix for r in remaining]
    ordered = layout_paragraphs(plans, obstacles, view)
    fragments = []
    continued = []
    with ExitStack() as stack:
        for p in ordered:
            more = True
            if p.capacity:
                doc, more = p.fragment(p.capacity)
                stack.enter_context(doc)
                target = fitz.Rect(p.source.x0,p.top,p.source.x1,p.top+p.capacity)
                fragments.append((p.key, target, doc))
            if more:
                height = p.measure()
                top = max([view.height+12]+[r.y1+3 for r in continued
                          if r.x0 < p.source.x1 and r.x1 > p.source.x0])
                target = fitz.Rect(p.source.x0,top,p.source.x1,top+height)
                doc, more = p.fragment(height)
                stack.enter_context(doc)
                if more:
                    raise ValueError("Incomplete translation paragraph")
                fragments.append((p.key, target, doc))
                continued.append(target)
        if continued:
            extend_page(page,max(r.y1 for r in continued)+12-view.height)
        rotation, unrotate = page.rotation, page.derotation_matrix
        # MuPDF's rotated page transformation omits the crop origin. Draw on
        # unrotated coordinates, then restore the displayed page orientation.
        page.set_rotation(0)
        try:
            for key, target, doc in fragments:
                page.show_pdf_page(target * unrotate, doc, 0, rotate=rotation)
                if key in bullets:
                    relative, pixmap = bullets.pop(key)
                    marker = relative * fitz.Matrix(1,0,0,1,target.x0,target.y0)
                    page.insert_image(marker*unrotate, pixmap=pixmap, rotate=rotation)
                anchors.append((key,target))
        finally:
            page.set_rotation(rotation)
    # Each hit region travels with this exact PDF, including partial previews
    # and paragraphs with a second region in the page continuation.
    for key, target in anchors:
        if key:
            annot = page.add_rect_annot(target * page.derotation_matrix)
            annot.set_info(title="translation-segment", subject="translation-segment", content=key)
            annot.set_border(width=0)
            annot.set_flags(fitz.PDF_ANNOT_IS_HIDDEN)
            annot.update()


def preview_page(store, identifier, number):
    """Use the export renderer on just this page, leaving pending text in place."""
    import fitz

    with fitz.open(store.file(identifier)) as original, fitz.open() as preview, store.db() as db:
        preview.insert_pdf(original, from_page=number-1, to_page=number-1)
        rows = _page_translations(db, identifier, number)
        _place_translations(preview[0], rows)
        preview.subset_fonts()
        return preview.tobytes(garbage=4, deflate=True)


def export_pdf(store, identifier, cancel):
    import fitz

    directory = store.bundle(identifier)
    staging = directory / f".translation-{uuid.uuid4().hex}.pdf"
    try:
        with fitz.open(store.file(identifier)) as pdf, store.db() as db:
            for page in pdf:
                if cancel.is_set():
                    return
                rows = _page_translations(db, identifier, page.number + 1)
                if any(row["translation"] is None for row in rows):
                    raise ValueError("incomplete translation")
                _place_translations(page, rows)
            pdf.set_metadata({**pdf.metadata, "title": store.get(identifier)["name"] + " · 中文译文"})
            pdf.subset_fonts()
            pdf.save(staging, garbage=4, deflate=True)
        if not cancel.is_set():
            os.replace(staging, directory / "中文译文.pdf")
    finally:
        staging.unlink(missing_ok=True)
