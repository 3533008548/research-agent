"""Model-facing lookup for exported figures and their source-page fallbacks."""
from paper_images import paper_image_catalog, format_image_catalog
from runtime_paths import get_runtime_paths


def handle_list_paper_images(args: dict, *, paper_store=None, **_kwargs) -> str:
    try:
        catalog, source = paper_image_catalog(args.get('paper_id_or_title', ''), paper_store, get_runtime_paths())
        return format_image_catalog(catalog, source, figure=args.get('figure'), page=args.get('page'),
                                    offset=args.get('offset', 0), limit=args.get('limit', 10))
    except (ValueError, OSError, TypeError) as exc:
        return f'❌ 图片目录查询失败：{exc}'
