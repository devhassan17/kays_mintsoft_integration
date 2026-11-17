import logging

_logger = logging.getLogger(__name__)


def find_phantom_bom_for_variant(env, variant, company_id):
    """Return a phantom mrp.bom for the given variant (or False) if mrp is installed."""
    if 'mrp.bom' not in env or not variant:
        return False
    Bom = env['mrp.bom'].sudo()
    bom = False
    try:
        found = Bom._bom_find(variant, company_id=company_id)
        if isinstance(found, dict):
            bom = found.get(variant) or False
        else:
            bom = found
    except TypeError:
        try:
            bom = Bom._bom_find(product=variant, company_id=company_id)
            if isinstance(bom, dict):
                bom = bom.get(variant) or False
        except Exception:
            bom = False
    except Exception as exc:
        _logger.debug("[Kays Pack] _bom_find failed for %s: %s", variant.display_name, exc)

    if bom and getattr(bom, 'type', None) == 'phantom':
        return bom

    return Bom.search([
        ('product_tmpl_id', '=', variant.product_tmpl_id.id),
        ('type', '=', 'phantom'),
        '|', ('product_id', '=', variant.id), ('product_id', '=', False),
        '|', ('company_id', '=', company_id), ('company_id', '=', False),
    ], order='product_id desc', limit=1)


def explode_phantom_bom(env, variant, qty, company_id):
    """Explode a phantom BoM for this variant into [(component_product, component_qty), ...]."""
    comps = []
    bom = find_phantom_bom_for_variant(env, variant, company_id)
    if not bom or getattr(bom, 'type', None) != 'phantom':
        return comps
    try:
        lines, _ops = bom.explode(variant, qty, picking_type=False)
        for bl, data in lines:
            p = bl.product_id
            q = data.get('qty', 0.0) if isinstance(data, dict) else 0.0
            if p and q and p.id != variant.id:
                comps.append((p, float(q)))
    except Exception as exc:
        _logger.debug("[Kays Pack] bom.explode fallback for %s: %s", variant.display_name, exc)

    if not comps:
        bom_qty = float(getattr(bom, 'product_qty', 1.0) or 1.0)
        factor = float(qty or 1.0) / bom_qty if bom_qty > 0 else float(qty or 1.0)
        for bl in bom.bom_line_ids:
            p = bl.product_id
            q = float(bl.product_qty or 0.0) * factor
            if p and q and p.id != variant.id:
                comps.append((p, q))
    return comps


def extract_oca_pack_lines(owner):
    """Extract pack lines from OCA product_pack or custom pack fields if present."""
    if not owner:
        return []
    for fname in ('pack_line_ids', 'pack_lines', 'pack_line_ids_variant'):
        lines = getattr(owner, fname, False)
        if lines:
            return list(lines)
    return []


def get_oca_pack_components(product, qty):
    """Return [(component_product, component_qty), ...] from OCA pack lines."""
    comps = []
    lines = extract_oca_pack_lines(product.product_tmpl_id) or extract_oca_pack_lines(product)
    for line in lines:
        comp = getattr(line, 'product_id', False) or getattr(line, 'item_id', False)
        line_qty = (
            getattr(line, 'qty', 0.0)
            or getattr(line, 'quantity', 0.0)
            or getattr(line, 'product_qty', 0.0)
            or getattr(line, 'uom_qty', 0.0)
            or 0.0
        )
        if comp and line_qty:
            comps.append((comp, float(line_qty) * float(qty or 1.0)))
    return comps


def is_pack_like(env, product, company_id):
    """Return True if product has OCA pack lines or a phantom (kit) BoM."""
    if not product:
        return False
    if extract_oca_pack_lines(product.product_tmpl_id) or extract_oca_pack_lines(product):
        return True
    return bool(find_phantom_bom_for_variant(env, product, company_id))


def get_pack_components(env, company_id, product, qty):
    """Return direct pack components from phantom BoM or OCA product_pack."""
    comps = explode_phantom_bom(env, product, qty, company_id)
    return comps or get_oca_pack_components(product, qty)


def expand_to_leaf_components(env, company_id, product, qty, depth=0, seen=None):
    """
    Recursively flatten pack / kit products until only leaf (non-pack) component products remain.
    Returns list of (component_product, component_qty) tuples.
    """
    if not product:
        return []
    if seen is None:
        seen = set()
    key = (product._name, product.id)
    if key in seen or depth > 8:
        _logger.warning("[Kays Pack] Recursion limit reached for %s", product.display_name)
        return []
    seen.add(key)

    if not is_pack_like(env, product, company_id):
        return [(product, float(qty or 0.0))]

    leaves = []
    for comp, comp_qty in get_pack_components(env, company_id, product, qty):
        if comp.id == product.id:
            continue
        leaves.extend(expand_to_leaf_components(env, company_id, comp, comp_qty, depth + 1, set(seen)))
    return leaves


def resolve_product_sku(product, mapping=None):
    """
    Resolve SKU for Mintsoft using strict priority:
      1) Active Kays Product Mapping (mapping.mintsoft_sku)
      2) product.default_code (Internal Reference)
      3) product.monta_sku (if defined on product from Monta integration)
      4) First supplierinfo product_code (seller_ids[:1].product_code)
      5) product.barcode
      6) product.product_tmpl_id.default_code
    """
    if mapping and getattr(mapping, 'mintsoft_sku', False) and mapping.mintsoft_sku.strip():
        return mapping.mintsoft_sku.strip()

    if not product:
        return ''

    dcode = getattr(product, 'default_code', False)
    if dcode and dcode.strip():
        return dcode.strip()

    msku = getattr(product, 'monta_sku', False)
    if msku and msku.strip():
        return msku.strip()

    seller_rs = getattr(product.sudo(), 'seller_ids', False)
    seller = seller_rs[:1] if seller_rs else False
    if seller and getattr(seller, 'product_code', False) and seller.product_code.strip():
        return seller.product_code.strip()

    barcode = getattr(product, 'barcode', False)
    if barcode and barcode.strip():
        return barcode.strip()

    tmpl = getattr(product, 'product_tmpl_id', False)
    if tmpl and getattr(tmpl, 'default_code', False) and tmpl.default_code.strip():
        return tmpl.default_code.strip()

    return ''

