"""Write a standalone, page-aligned Chinese PDF alongside its library source."""
import html
import json
import os
from papers.translation_layout import INLINE_TOKEN, inline_layout, identifier_strokes, native_lines, lines_in, typography
import uuid


_BODY_LINE_HEIGHT_PT = 15

# A transparent pixel reserves each formula's width during wrapping. Its
# bottom edge sits on the same baseline as the surrounding text in MuPDF.
_SPACER = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAACXBIWXMAAA7EAAAOxAGVKw4bAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=")


def _place_formula_paragraph(page, rect, markup, css, formulas):
    import fitz

    width, height = (rect.height, rect.width) if page.rotation % 180 else (rect.width, rect.height)
    story = fitz.Story(markup, user_css=css)
    fit = story.fit_scale(fitz.Rect(0, 0, width, height), scale_min=1)
    with story.write_with_links(lambda *_: (fit.rect, fit.rect, None)) as doc:
        paragraph = doc[0]
        slots = paragraph.get_image_info()
        for slot, (image, area, ascent, fixed_height) in zip(slots, formulas, strict=True):
            bounds = fitz.Rect(slot["bbox"])
            em = bounds.height * 2   # The reserved image is half an em tall.
            baseline = bounds.y1
            # Keep tall stacked scripts within one line, including at the
            # top and bottom of the paragraph. Preserve the glyph aspect ratio.
            bottom = min(paragraph.rect.height, baseline + .32 * em)
            line_height = fixed_height if fixed_height is not None else 1.08 * em
            top = max(0, baseline + .32 * em - line_height)
            scale = min(bounds.width / area.width, (bottom - top) / area.height)
            w, h = area.width * scale, area.height * scale
            x = (bounds.x0 + bounds.x1 - w) / 2
            y = max(top, min(baseline - ascent * scale, bottom - h))
            paragraph.insert_image(fitz.Rect(x, y, x + w, y + h), stream=image)
        page.show_pdf_page(rect, doc, 0, rotate=page.rotation)


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
    group_sizes = {}
    view = page.rect
    lines = native_lines(page)
    _, clips, _ = inline_layout(lines)
    strokes = identifier_strokes(page, page.get_drawings())
    # Preserve the source typography where the translation is identical,
    # including standalone equations and symbols inside diagrams.
    for row in rows:
        text = row["translation"]
        if text.strip() == row["source"].strip():
            continue
        x,y,r,b = json.loads(row["bounds"])
        rect = fitz.Rect(x*view.width,y*view.height,r*view.width,b*view.height) * page.derotation_matrix
        if rect.is_empty:
            continue
        # Keep original raster images and vector lines while removing the replaced text.
        native = lines_in(rect, lines)
        size, bold, centered, indent = typography(rect, native)
        # Justified body lines can have near-identical centers. Alignment
        # requires title evidence as well as geometry.
        centered = ((row["kind"] == "heading" or size >= 15) and
                    (centered or abs((rect.x0+rect.x1-page.cropbox.width)/2) < .65*size))
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
            formulas.append((pix.tobytes("png"), area, ascent, fixed_height))
            return f'<img style="width:{area.width / size}em;height:0.5em" src="{_SPACER}">'
        markup = INLINE_TOKEN.sub(reserve_formula, markup)
        group = None
        key = row["id"] if "id" in row.keys() else ""
        parent, _, item = key.rpartition("-item")
        if parent and item.isdigit():
            group = (parent, size)
            width, height = (rect.height, rect.width) if page.rotation % 180 else (rect.width, rect.height)
            fit = fitz.Story(markup, user_css=css).fit_scale(fitz.Rect(0,0,width,height), scale_min=1)
            group_sizes[group] = min(group_sizes.get(group, size), size/fit.parameter)
        plans.append((rect, markup, css, group, formulas))
    if plans:
        # Some publisher headers rely on clipped text. MuPDF redaction can
        # expose the clipped glyphs well outside the changed paragraphs.
        # Restore untouched margin strips only if their appearance changed.
        margins = [fitz.Rect(0, 0, view.width, view.height * .045),
                   fitz.Rect(0, view.height * .965, view.width, view.height)]
        zoom = min(3, 3000 / max(view.width, view.height))
        matrix = fitz.Matrix(zoom, zoom)
        margins = [(clip, page.get_pixmap(clip=clip, matrix=matrix)) for clip in margins
                   if not any(clip.intersects(rect * page.rotation_matrix) for rect, *_ in plans)]
        # Remove only confirmed identifier strokes inside replaced prose. A
        # graphics-wide redaction here would also erase fraction bars and grids.
        removed = False
        for stroke, _, _ in strokes:
            if any(rect.contains((stroke.tl+stroke.br)/2) for rect, *_ in plans):
                page.add_redact_annot(stroke + (-1,-1,1,1), fill=False)
                removed = True
        if removed:
            page.apply_redactions(images=0, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=fitz.PDF_REDACT_TEXT_NONE)
        for rect, *_ in plans:
            page.add_redact_annot(rect, fill=False)
        page.apply_redactions(images=0, graphics=0)
        for clip, image in margins:
            if page.get_pixmap(clip=clip, matrix=matrix).digest != image.digest:
                page.insert_image(clip * page.derotation_matrix, pixmap=image, rotate=page.rotation,
                                  keep_proportion=False)
    for rect, markup, css, group, formulas in plans:
        if group:
            css += f"body{{font-size:{group_sizes[group]}pt}}"
        if formulas:
            _place_formula_paragraph(page, rect, markup, css, formulas)
            continue
        free, _ = page.insert_htmlbox(rect, markup, css=css, rotate=page.rotation)
        if free < 0:
            raise ValueError("translation text did not fit")


def preview_page(store, identifier, number):
    """Use the export renderer on just this page, leaving pending text in place."""
    import fitz

    with fitz.open(store.file(identifier)) as original, fitz.open() as preview, store.db() as db:
        preview.insert_pdf(original, from_page=number-1, to_page=number-1)
        rows = _page_translations(db, identifier, number)
        _place_translations(preview[0], [row for row in rows if row["translation"] is not None])
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
