"""Recover page structures that a PDF producer may split or merge into text blocks."""
import re
from collections import Counter
from itertools import groupby
from statistics import median

import fitz


SECTION_HEADING = re.compile(r"[IVXLCDM]+\.\s+[A-Z][A-Z\s,&–-]*")


def native_lines(page):
    blocks = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]
    return [line for block in blocks for line in block.get("lines", []) if line["spans"]]


def line_text(line):
    return "".join(span["text"] for span in line["spans"]).strip()


def lines_in(rect, lines):
    return [line for line in lines if rect.contains((fitz.Rect(line["bbox"]).tl + fitz.Rect(line["bbox"]).br) / 2)]


def ruled_tables(lines, paths):
    """Booktabs tables may have no vertical borders and three disconnected rules."""
    rules = sorted((path["rect"] for path in paths if path["rect"].width > 60 and path["rect"].height < 2),
                   key=lambda rect: rect.y0)
    regions = []
    for line in lines:
        if not re.fullmatch(r"(?:table|表)\s*(?:[IVXLCDM]+|\d+)[.:：]?", line_text(line), re.I):
            continue
        caption = fitz.Rect(line["bbox"])
        center = (caption.x0 + caption.x1) / 2
        top = next((r for r in rules if 0 <= r.y0-caption.y1 < 70 and r.x0 < center < r.x1), None)
        if top is None:
            continue
        borders = [top]
        for r in rules:
            if r.y0 <= borders[-1].y0 + 2 or abs(r.x0-top.x0)>3 or abs(r.x1-top.x1)>3:
                continue
            if r.y0-borders[-1].y0 > max(100, top.width / 2):
                break
            # A wide gap containing full-width prose separates distinct tables.
            between = [l for l in lines if borders[-1].y0 < l["bbox"][1] < r.y0
                       and top.x0 <= l["bbox"][0] <= top.x1]
            if not between or any(l["bbox"][2]-l["bbox"][0] > .8*top.width for l in between):
                break
            borders.append(r)
        if len(borders) >= 3:
            regions.append(fitz.Rect(top.x0-1, caption.y0-1, top.x1+1, borders[-1].y1+1))
    return regions


def typography(rect, lines):
    spans = [s for line in lines for s in line["spans"] if s["text"].strip()]
    if not spans:
        return 10, False, False, 0
    weights = Counter()
    for span in spans:
        weights[round(span["size"]*2)/2] += len(span["text"].strip())
    size = weights.most_common(1)[0][0]
    bold = sum(len(s["text"]) for s in spans if s["flags"] & 16) > sum(len(s["text"]) for s in spans)/2
    centers = [(l["bbox"][0]+l["bbox"][2])/2 for l in lines]
    centered = len(lines)>1 and max(centers)-min(centers)<size*.65 and max(l["bbox"][0] for l in lines)-rect.x0>size*.5
    indent = lines[0]["bbox"][0]-median(l["bbox"][0] for l in lines[1:]) if len(lines)>1 else 0
    return size, bold, centered, indent if .4*size < indent < 3*size else 0


def structural_elements(page, elements, lines, paths):
    marks = [p["rect"] for p in paths if 1 <= p["rect"].width <= 8 and 1 <= p["rect"].height <= 8]
    result = []
    previous_title_size = None
    for element in elements:
        box = fitz.Rect(element["bbox"])
        native = lines_in(box, lines)
        if page.number == 0:
            roles = [s for l in native for s in l["spans"] if MEMBER.fullmatch(s["text"].strip())]
            if roles:
                result.extend({**element, "id":f"{element['id']}-role{i}", "kind":"membership",
                               "text":s["text"], "bbox":s["bbox"]} for i,s in enumerate(roles))
                previous_title_size = None
                continue
        size, _, _, _ = typography(box, native)
        center = page.cropbox.width/2
        centered_title = native and size >= 15 and all(abs((l["bbox"][0]+l["bbox"][2])/2-center)<size*.65 for l in native)
        if centered_title and size == previous_title_size:
            last = result[-1]
            old = fitz.Rect(last["bbox"])
            if -.35*size <= box.y0-old.y1 < .65*size:
                result[-1] = {**last, "text":last["text"]+" "+element["text"], "bbox":list(old|box), "is_heading":True}
                continue
        previous_title_size = size if centered_title else None
        groups = []
        left = min((l["bbox"][0] for l in native), default=box.x0)
        for line in native:
            x,y,_,bottom = line["bbox"]
            marker = any(0 <= x-m.x1 < size*1.8 and y <= (m.y0+m.y1)/2 <= bottom for m in marks)
            paragraph = (groups and not centered_title and .5*size < x-left < 3*size
                         and groups[-1][-1]["bbox"][2] < box.x1-size
                         and y > groups[-1][-1]["bbox"][1]+size*.75)
            after_heading = (groups and len(groups[-1]) == 1 and
                             SECTION_HEADING.fullmatch(line_text(groups[-1][0])))
            if not groups or marker or paragraph or after_heading:
                groups.append([])
            groups[-1].append(line)
        if len(groups) < 2:
            result.append(element)
            continue
        for i, group in enumerate(groups):
            region = fitz.Rect(group[0]["bbox"])
            for line in group[1:]:
                region |= fitz.Rect(line["bbox"])
            value = " ".join(line_text(line) for line in group)
            result.append({**element, "id":f"{element['id']}-item{i}", "bbox":list(region),
                           "text":value, "is_heading": bool(SECTION_HEADING.fullmatch(value))
                           or bool(element.get("is_heading"))})
    return result

def identifier_strokes(page, paths):
    """A TeX underscore may be a short stroked path in the gap between two words."""
    strokes = [p["rect"] for p in paths if p["type"] == "s" and len(p["items"]) == 1
               and p["items"][0][0] == "l" and p["rect"].height < .15
               and 1 < p["rect"].width < 20 and p["width"] <= 1.5]
    if not strokes:
        return []
    words = page.get_text("words")
    found = []
    for a,b in zip(words, words[1:]):
        if a[5:7] != b[5:7]:
            continue
        left = re.search(r"[a-zA-Z0-9_]+$", a[4])
        right = re.match(r"[a-zA-Z0-9_]+", b[4])
        if not left or not right:
            continue
        for r in strokes:
            if (abs(a[2]-r.x0) <= .85 and abs(b[0]-r.x1) <= .85
                    and a[1]+.65*(a[3]-a[1]) <= r.y0 <= a[3]+1
                    and b[1]+.65*(b[3]-b[1]) <= r.y0 <= b[3]+1):
                found.append((r, left[0], right[0]))
                break
    return found


def restore_identifiers(text, bounds, strokes):
    for r,left,right in strokes:
        if bounds.contains((r.tl+r.br)/2):
            pattern = r"(?<![A-Za-z0-9])"+re.escape(left)+r"\s+"+re.escape(right)+r"(?![A-Za-z0-9])"
            text = re.sub(pattern, left+"_"+right, text)
    return text


def math_line(line):
    """Preserve the source drawing when plain Unicode cannot represent its typography."""
    spans = [s for s in line["spans"] if s["text"].strip(" •·▪")]
    if any(re.search(r"CMR\d", s["font"]) for s in spans) and re.search(r"[=≤≥∑∏]", line_text(line)):
        return True
    return any(re.search(r"CMMI|CMSY|CMEX|Math|Symbol", s["font"], re.I)
               or "\ufffd" in s["text"] or any(0xe000 <= ord(c) <= 0xf8ff for c in s["text"])
               or (s["flags"] & 1 and re.search(r"[\w±+−]", s["text"]))
               for s in spans)

INLINE_TOKEN = re.compile(r"⟦(\d+\.\d+)\|([^⟧]*)⟧")
MEMBER = re.compile(r",?\s*(?:(?:Graduate Student|Senior|Student|Life Senior|Life Fellow|Life) )?(?:Member|Fellow), IEEE", re.I)


def membership_translation(source):
    role = source.lower().split(", ieee")[0].strip(", ")
    roles = {"graduate student member":"研究生会员", "student member":"学生会员",
             "senior member":"高级会员", "member":"会员", "fellow":"会士",
             "life senior member":"终身高级会员", "life member":"终身会员", "life fellow":"终身会士"}
    if role in roles:
        return ("，" if source.lstrip().startswith(",") else "")+"IEEE "+roles[role]
    return None


def _formula_span(span):
    if re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{2,}", span["text"]):
        return False
    return math_line({"spans":[span]}) or bool(re.search(r"CMR\d", span["font"]))


def inline_layout(lines):
    """Keep display math in place; anchor math embedded in prose to its native glyphs."""
    prose = [l for l in lines if len(re.findall(r"[A-Za-z]{2,}", line_text(l))) >= 3
             and any(re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", s["text"]) or
                     (not math_line({"spans":[s]}) and len(s["text"].strip()) >= 4)
                     for s in l["spans"])]
    prose_ids = {id(l) for l in prose}
    text, clips, display = {}, {}, []
    for i,l in enumerate(lines):
        if not math_line(l):
            continue
        box = fitz.Rect(l["bbox"])
        # A PDF can put a subscript in a separate line at the same baseline.
        same_row = any(abs(box.y1-p["bbox"][3]) < .65*box.height and
                       min(box.y1,p["bbox"][3])-max(box.y0,p["bbox"][1]) > .35*box.height and
                       min(abs(box.x0-p["bbox"][2]), abs(box.x1-p["bbox"][0])) < 7 for p in prose)
        if id(l) not in prose_ids and not same_row:
            display.append(box)
            continue
        parts = []
        for j,(is_math, seq) in enumerate(groupby(l["spans"], _formula_span)):
            spans = list(seq)
            value = "".join(s["text"] for s in spans)
            # Publishers sometimes switch to TeX math fonts for the dots
            # and plus signs in a software version, on an ordinary baseline.
            if (is_math and re.fullmatch(r"[\d.+\s]+", value)
                    and not any(s["flags"] & 1 for s in spans)
                    and len({round(s["origin"][1], 1) for s in spans}) == 1):
                is_math = False
            if is_math and re.search(r"[\w\u0370-\u03ff\u2000-\u23ff\ufffd]", value):
                area = fitz.Rect(spans[0]["bbox"])
                for s in spans[1:]:
                    area |= fitz.Rect(s["bbox"])
                label = "".join(c for c in value.strip() if c.isprintable() and c not in "⟦⟧|") or "公式"
                token = f"⟦{i}.{j}|{label}⟧"
                clips[token] = area
                parts.append(" "+token+" ")
            else:
                parts.append(value)
        text[id(l)] = "".join(parts).strip()
    # TeX can emit the superscript and subscript of one variable in two
    # different native lines. Move their original glyphs as a single image.
    for first,second in zip(lines,lines[1:]):
        a,b = text.get(id(first),""),text.get(id(second),"")
        matches = list(INLINE_TOKEN.finditer(a))
        tail = matches[-1] if matches else None
        head = INLINE_TOKEN.match(b)
        if tail and head and tail.end() == len(a):
            x,y = clips[tail[0]],clips[head[0]]
            if x.intersects(y) and abs(x.x1-y.x0)<8:
                token = f"⟦{tail[1]}|{tail[2]}{head[2]}⟧"
                clips[token] = x|y
                text[id(first)] = a[:tail.start()]+token
                text[id(second)] = b[head.end():].lstrip()
    return text, clips, display


def join_continuations(elements):
    """Join neighboring pieces of a wrapped sentence within one column."""
    result = []
    for e in elements:
        if result and e["kind"] == "text" and not e.get("is_heading") and not result[-1].get("is_heading"):
            last = result[-1]
            a,b = fitz.Rect(last["bbox"]),fitz.Rect(e["bbox"])
            if (last["kind"] == "text" and "-item" not in e["id"] and "-item" not in last["id"]
                    and abs(a.x0-b.x0)<13 and abs(a.x1-b.x1)<15 and a.width>80
                    and -10 < b.y0-a.y1 < 5 and b.y0>a.y0 and b.y1>a.y1
                    and not re.search(r"[.!?:;。]$",last["text"].strip())
                    and (re.match(r"[a-z]",e["text"]) or b.y0<a.y1)):
                result[-1] = {**last, "text": last["text"]+" "+e["text"], "bbox":list(a|b)}
                continue
        result.append(e)
    return result
