"""Discover PDF figures and give every model-visible entry a real local asset.

Captioned figures use a rendered source page. This includes vector drawings,
labels and all panels without pretending an uncertain crop is the whole figure.
Uncaptioned embedded images retain their own regions. The manifest is content
addressed and page bounded; a legacy filename glob is never a cache hit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import pymupdf

IMAGE_VERSION = 1
_CAPTION = re.compile(r"^(?:Figure|Fig\.?|图)\s*(\d+)\b\s*(?:[.:：]|$)", re.I)
_LOCATOR = re.compile(r"(?:(?<![a-z])Figure\s*|(?<![a-z])Fig\.?\s*|图\s*)(\d+|[一二三四五六七八九十]+)(?!\d|\.\d|[一二三四五六七八九十百])", re.I)
_DIGITS = {ch: n for n, ch in enumerate('零一二三四五六七八九')}


def figure_numbers(text: str) -> set[str]:
    values = set()
    for raw in _LOCATOR.findall(text):
        if raw.isdecimal():
            values.add(str(int(raw)))
        elif '十' in raw and raw.count('十') == 1:
            left, right = raw.split('十')
            if len(left) <= 1 and len(right) <= 1:
                values.add(str((_DIGITS.get(left, 1) * 10) + _DIGITS.get(right, 0)))
        elif raw in _DIGITS:
            values.add(str(_DIGITS[raw]))
    return values


def figure_regions(page) -> list[dict]:
    lines = [line for block in page.get_text('dict', flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)['blocks']
             for line in block.get('lines', [])]
    figures = []
    seen = set()
    for line in lines:
        caption = ''.join(span['text'] for span in line.get('spans', [])).strip()
        match = _CAPTION.match(caption)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        figures.append({'label': 'Figure ' + match.group(1), 'number': match.group(1),
                        'caption': caption, 'bbox': list(page.rect),
                        'caption_bbox': list(line['bbox']), 'asset_kind': 'page'})
    # With a caption, rendering its page already retains every raster/vector
    # panel. Do not duplicate individual sub-images under guessed figure labels.
    if not figures:
        for info in page.get_image_info():
            bbox = list(info['bbox'])
            if pymupdf.Rect(bbox).get_area() >= 5000:
                figures.append({'label': f'未编号图片 {len(figures) + 1}', 'number': '',
                                'caption': '', 'bbox': bbox, 'asset_kind': 'region'})
    return figures


def _render_page_region(page, bbox, target: Path) -> None:
    if target.is_file():
        return
    rect = pymupdf.Rect(bbox) & page.rect
    dpi = min(180, max(36, int(72 * (12_000_000 / max(rect.get_area(), 1)) ** 0.5)))
    data = page.get_pixmap(clip=rect, dpi=dpi, colorspace=pymupdf.csRGB, alpha=False).tobytes('png')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def extract_pdf_images(source: Path, output_dir: Path, max_pages: int = 100, *, render: bool = True) -> dict:
    source, output_dir = Path(source).resolve(), Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pymupdf.open(source) as pdf:
        count = min(len(pdf), max(1, min(100, int(max_pages))))
        records = []
        for index in range(count):
            page = pdf[index]
            for ordinal, figure in enumerate(figure_regions(page), 1):
                label = 'Figure' + figure['number'] if figure['number'] else f'image{ordinal}'
                # Include figure number in the basename so old map attachment
                # code and humans can still resolve it; geometry is hashed.
                region = hashlib.sha256(repr(figure['bbox']).encode()).hexdigest()[:10]
                target = output_dir / f'{digest[:16]}_images_v{IMAGE_VERSION}_p{index+1:03d}_{label}_{region}.png'
                if render:
                    _render_page_region(page, figure['bbox'], target)
                records.append({**figure, 'page': index + 1, 'asset_path': str(target)})
        manifest = {'version': IMAGE_VERSION, 'source_sha256': digest, 'total_pages': len(pdf),
                    'processed_pages': count, 'images': records}
    # The directory is discoverable even when history has been compressed.
    manifest_path = output_dir / f'{digest[:16]}_images_v{IMAGE_VERSION}_{count}.json'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output_dir, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, manifest_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return manifest


def paper_image_catalog(selector: str, paper_store, paths, *, render: bool = False) -> tuple[dict, Path]:
    from tools.search import _resolve_scope
    from paper_artifacts import load_document_map

    if not str(selector or '').strip():
        raise ValueError('请提供论文标题或 paper_id')
    if not paper_store:
        raise ValueError('请先通过 read_pdf 关联论文原文件')
    scope, error = _resolve_scope(paper_store, selector)
    if error or not scope or len(scope) != 1:
        raise ValueError(error or '请一次指定一篇论文')
    mapping = load_document_map(paths, scope[0]) or {}
    source_file = mapping.get('source_file')
    if not source_file:
        raise ValueError('该论文缺少原文件映射，请先 read_pdf 关联原文件')
    source = (paths.papers_dir / source_file).resolve()
    # Support maps written before library/ was included in stored filenames.
    if not source.is_file() and Path(source_file).name == source_file:
        source = (paths.papers_dir / 'library' / source_file).resolve()
    if not source.is_relative_to(paths.papers_dir.resolve()) or not source.is_file():
        raise ValueError('原 PDF 不在当前论文目录中，无法恢复图片；不猜测其他论文的图片')
    catalog = extract_pdf_images(source, paths.images_dir, render=render)
    catalog.update(paper_id=scope[0], title=mapping.get('title') or selector)
    return catalog, source


def select_images(catalog: dict, *, figure=None, page=None) -> list[dict]:
    records = catalog['images']
    if page is not None:
        page = int(page)
        if not 1 <= page <= catalog['processed_pages']:
            raise ValueError(f"页码超出已检查范围 1–{catalog['processed_pages']}")
        records = [entry for entry in records if entry['page'] == page]
    if figure is not None:
        value = str(figure).strip()
        numbers = {str(int(value))} if value.isdecimal() else figure_numbers(value)
        if len(numbers) != 1:
            raise ValueError('请提供一个图号，例如 4、Fig. 4 或图四')
        records = [entry for entry in records if entry['number'] in numbers]
        if not records:
            raise ValueError('未定位到该图号的图注；可用 list_paper_images 查看目录，或指定原文 page 查看整页')
    return records


def render_image_entry(source: Path, entry: dict) -> Path:
    target = Path(entry['asset_path'])
    with pymupdf.open(source) as pdf:
        _render_page_region(pdf[entry['page'] - 1], entry['bbox'], target)
    return target


def resolve_paper_image(args, paper_store, paths) -> tuple[Path, str]:
    catalog, source = paper_image_catalog(args.get('paper_id_or_title', ''), paper_store, paths)
    figure, page = args.get('figure'), args.get('page')
    if figure is None and page is None:
        raise ValueError('按论文查看图片时还需 figure 或 page；也可先 list_paper_images 查看目录')
    entries = select_images(catalog, figure=figure, page=page)
    if figure is not None:
        if len(entries) != 1:
            raise ValueError('图号出现在多页，请同时指定 page')
        entry = entries[0]
    else:
        with pymupdf.open(source) as pdf:
            entry = {'page': int(page), 'bbox': list(pdf[int(page)-1].rect), 'caption': '',
                     'label': '原页', 'asset_kind': 'page',
                     'asset_path': str(paths.images_dir / f"{catalog['source_sha256'][:16]}_images_v{IMAGE_VERSION}_p{int(page):03d}_page.png")}
    target = render_image_entry(source, entry)
    return target, image_source_note(entry, catalog['title'])


def image_source_note(entry: dict, title: str = '') -> str:
    note = f"{title} · p.{entry['page']} · {entry['label']}。{entry.get('caption', '')}"
    if entry['asset_kind'] == 'page':
        note += '这是整页原图；请只分析指定图号，勿混入相邻图表。'
    return note


def asset_source_note(path: Path) -> str:
    """Keep the target figure when a model uses the listed path instead of its ID."""
    match = re.match(r'([a-f0-9]{16}_images_v\d+)_p', path.name)
    if not match:
        return ''
    for manifest in sorted(path.parent.glob(match.group(1) + '_*.json')):
        try:
            catalog = json.loads(manifest.read_text(encoding='utf-8'))
            for entry in catalog.get('images', []):
                if Path(entry['asset_path']).resolve() == path.resolve():
                    return image_source_note(entry)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return ''


def format_image_catalog(catalog, source, *, figure=None, page=None, offset=0, limit=10) -> str:
    entries = select_images(catalog, figure=figure, page=page)
    offset, limit = max(0, int(offset)), max(1, min(10, int(limit)))
    rows = []
    used = 0
    for entry in entries[offset:offset + limit]:
        kind = '整页原图（包含该图）' if entry['asset_kind'] == 'page' else '图片区域'
        row = f"{entry['label']} · p.{entry['page']} · {kind}\n{entry['caption'][:160]}\n图片路径：{entry['asset_path']}"
        if rows and used + len(row) > 6000:
            break
        render_image_entry(source, entry)
        rows.append(row)
        used += len(row)
    end = offset + len(rows)
    more = f'；后续目录请传 offset={end}' if end < len(entries) else ''
    return (f"🖼️ 图片目录 — {catalog['title']}（{catalog['paper_id']}）\n"
            f"已检查 {catalog['processed_pages']}/{catalog['total_pages']} 页；匹配 {len(entries)} 项，返回 {len(rows)} 项{more}。\n"
            '可将图片路径传给 describe_image；也可直接传 paper_id_or_title 和 figure/page，无需手动截图或猜路径。\n\n'
            + '\n\n'.join(rows))
