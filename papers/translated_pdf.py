"""Write a standalone, page-aligned Chinese PDF alongside its library source."""
import html
import json
import os
import re
from statistics import median
import uuid


def _content(kind, text):
    if kind == "table":
        rows = [line.strip("| ").split("|") for line in text.splitlines() if "|" in line]
        cells = ["<tr>" + "".join("<td>" + html.escape(c.strip()) + "</td>" for c in row) + "</tr>"
                 for row in rows if not all(re.fullmatch(r"[-: ]+", c) for c in row)]
        if cells:
            return "<table>" + "".join(cells) + "</table>"
    body = html.escape(text).replace("\n", "<br>")
    return "<b>" + body + "</b>" if kind == "heading" else body


def export_pdf(store, identifier, cancel):
    import fitz

    directory = store.bundle(identifier)
    staging = directory / f".translation-{uuid.uuid4().hex}.pdf"
    try:
        with fitz.open(store.file(identifier)) as pdf, store.db() as db:
            for page in pdf:
                if cancel.is_set():
                    return
                plans = []
                view = page.rect
                lines = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]
                spans = [(fitz.Rect(s["bbox"]), s["size"]) for block in lines
                         for line in block.get("lines", []) for s in line["spans"] if s["text"].strip()]
                # Preserve the source typography where the translation is identical,
                # including standalone equations and symbols inside diagrams.
                for row in db.execute("""SELECT source,translation,kind,bounds FROM translation_segments
                                         WHERE document_id=? AND page=? ORDER BY rowid""", (identifier, page.number + 1)):
                    text = row["translation"]
                    if text is None:
                        raise ValueError("incomplete translation")
                    if text.strip() == row["source"].strip():
                        continue
                    x,y,r,b = json.loads(row["bounds"])
                    rect = fitz.Rect(x*view.width,y*view.height,r*view.width,b*view.height) * page.derotation_matrix
                    if rect.is_empty:
                        continue
                    # Keep original raster images and vector lines while removing the replaced text.
                    page.add_redact_annot(rect, fill=False)
                    sizes = [size for box, size in spans if rect.contains((box.tl + box.br) * .5)]
                    size = median(sizes) if sizes else (12 if row["kind"] == "heading" else 10)
                    plans.append((rect, row["kind"], text, min(42, max(6, size))))
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
                    page.apply_redactions(images=0, graphics=0)
                    for clip, image in margins:
                        if page.get_pixmap(clip=clip, matrix=matrix).digest != image.digest:
                            page.insert_image(clip * page.derotation_matrix, pixmap=image, rotate=page.rotation,
                                              keep_proportion=False)
                for rect, kind, text, size in plans:
                    css = (f"*{{margin:0;padding:0}} body{{font-family:serif;font-size:{size}pt;line-height:1.22}}"
                           "table{border-collapse:collapse;width:100%}td{border:.4pt solid #999;padding:1pt}")
                    if kind == "ocr_text":
                        page.draw_rect(rect, color=None, fill=(1,1,1))
                    free, _ = page.insert_htmlbox(rect, '<div lang="zh-Hans">'+_content(kind,text)+'</div>',
                                                  css=css, rotate=page.rotation)
                    if free < 0:
                        raise ValueError("translation text did not fit")
            pdf.set_metadata({**pdf.metadata, "title": store.get(identifier)["name"] + " · 中文译文"})
            pdf.subset_fonts()
            pdf.save(staging, garbage=4, deflate=True)
        if not cancel.is_set():
            os.replace(staging, directory / "中文译文.pdf")
    finally:
        staging.unlink(missing_ok=True)
