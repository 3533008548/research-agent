"""Write a standalone, page-aligned Chinese PDF alongside its library source."""
import html
import json
import os
from papers.translation_layout import identifier_strokes, native_lines, lines_in, typography
import uuid


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
        if size >= 15 and abs((rect.x0+rect.x1-page.cropbox.width)/2) < .65*size:
            centered = True
        css = (f"*{{margin:0;padding:0}} body{{margin:0;font-family:serif;font-size:{size}pt;line-height:1.08}}"
               f"div{{text-align:{'center' if centered else 'left'};text-indent:{indent}pt;"
               f"font-weight:{'bold' if bold else 'normal'}}}")
        markup = '<div lang="zh-Hans">'+_content(row["kind"],text)+'</div>'
        group = None
        key = row["id"] if "id" in row.keys() else ""
        parent, _, item = key.rpartition("-item")
        if parent and item.isdigit():
            group = (parent, size)
            width, height = (rect.height, rect.width) if page.rotation % 180 else (rect.width, rect.height)
            fit = fitz.Story(markup, user_css=css).fit_scale(fitz.Rect(0,0,width,height), scale_min=1)
            group_sizes[group] = min(group_sizes.get(group, size), size/fit.parameter)
        plans.append((rect, markup, css, group))
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
    for rect, markup, css, group in plans:
        if group:
            css += f"body{{font-size:{group_sizes[group]}pt}}"
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
