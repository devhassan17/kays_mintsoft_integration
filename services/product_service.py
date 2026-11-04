import logging
from odoo import fields, _
from odoo.exceptions import UserError

from .mintsoft_client import MintsoftClient, MintsoftAPIError

_logger = logging.getLogger(__name__)


class ProductService:
    """Domain service for Mintsoft <-> Odoo Product & SKU synchronization."""

    def __init__(self, env):
        self.env = env

    def fetch_and_map_products(self, instance, since_last_updated=None, max_pages=25):
        """
        Fetch all products from Mintsoft (GET /api/Product/List) and synchronize
        kays.mintsoft.product.mapping records for the connection's company.
        """
        if not instance.active or not instance.enable_integration:
            raise UserError(_("Integration is disabled on connection '%s'.") % instance.display_name)
        if not instance.enable_product_import:
            raise UserError(_("Product Import / Mapping Sync is disabled on connection '%s'.") % instance.display_name)

        client = MintsoftClient(instance)
        all_remote_products = []

        for page_no in range(1, max_pages + 1):
            params = {'PageNo': page_no, 'Limit': 100}
            if since_last_updated:
                params['SinceLastUpdated'] = since_last_updated
            batch = client.request(
                'GET',
                '/api/Product/List',
                params=params,
                operation='list_products',
            )
            if not isinstance(batch, list) or not batch:
                break
            all_remote_products.extend(batch)
            if len(batch) < 100:
                break

        Mapping = self.env['kays.mintsoft.product.mapping']
        Product = self.env['product.product'].with_company(instance.company_id)

        # Build lookup of Odoo products by SKU for this company
        odoo_products = Product.search([
            ('company_id', 'in', [False, instance.company_id.id]),
            ('active', '=', True),
        ])
        sku_to_odoo_products = {}
        for prod in odoo_products:
            code = (prod.default_code or '').strip()
            if code:
                sku_to_odoo_products.setdefault(code, []).append(prod)

        seen_skus = set()
        stats = {'matched': 0, 'missing_in_odoo': 0, 'duplicate_sku': 0, 'missing_in_mintsoft': 0}

        for ms_prod in all_remote_products:
            if not isinstance(ms_prod, dict):
                continue
            ms_id = int(ms_prod.get('ID') or 0)
            ms_sku = (ms_prod.get('SKU') or '').strip()
            if not ms_sku:
                continue
            seen_skus.add(ms_sku)

            matched_odoo_list = sku_to_odoo_products.get(ms_sku, [])
            if len(matched_odoo_list) == 1:
                odoo_prod = matched_odoo_list[0]
                status = 'matched'
                err_msg = False
                stats['matched'] += 1
            elif len(matched_odoo_list) > 1:
                odoo_prod = matched_odoo_list[0]
                status = 'duplicate_sku'
                err_msg = _("Multiple Odoo products (%d) share SKU '%s'.") % (len(matched_odoo_list), ms_sku)
                stats['duplicate_sku'] += 1
            else:
                odoo_prod = self.env['product.product']
                status = 'missing_in_odoo'
                err_msg = _("SKU '%s' exists in Mintsoft (ID %s) but not in Odoo.") % (ms_sku, ms_id)
                stats['missing_in_odoo'] += 1

            existing = Mapping.search([
                ('instance_id', '=', instance.id),
                '|',
                ('mintsoft_product_id', '=', ms_id),
                ('mintsoft_sku', '=', ms_sku),
            ], limit=1)

            barcode = (ms_prod.get('EAN') or ms_prod.get('UPC') or '').strip()
            vals = {
                'instance_id': instance.id,
                'company_id': instance.company_id.id,
                'product_id': odoo_prod.id if odoo_prod else (existing.product_id.id if existing else False),
                'odoo_sku': odoo_prod.default_code if odoo_prod else (existing.odoo_sku if existing else ''),
                'odoo_barcode': odoo_prod.barcode if odoo_prod else '',
                'mintsoft_product_id': ms_id,
                'mintsoft_sku': ms_sku,
                'mintsoft_name': (ms_prod.get('Name') or ms_sku)[:250],
                'mintsoft_description': (ms_prod.get('Description') or '')[:1000],
                'mintsoft_barcode': barcode[:100],
                'mintsoft_weight': float(ms_prod.get('Weight') or 0.0),
                'mintsoft_height': float(ms_prod.get('Height') or 0.0),
                'mintsoft_width': float(ms_prod.get('Width') or 0.0),
                'mintsoft_depth': float(ms_prod.get('Depth') or 0.0),
                'mintsoft_volume': float(ms_prod.get('Volume') or 0.0),
                'is_bundle': bool(ms_prod.get('Bundle')),
                'discontinued_in_mintsoft': bool(ms_prod.get('DisCont')),
                'mapping_status': 'matched' if (existing and existing.product_id and status == 'missing_in_odoo') else status,
                'last_sync_at': fields.Datetime.now(),
                'last_error': False if (existing and existing.product_id and status == 'missing_in_odoo') else err_msg,
            }
            if existing:
                if existing.mapping_status == 'disabled':
                    vals['mapping_status'] = 'disabled'
                existing.write(vals)
            else:
                Mapping.create(vals)

        # Also check Odoo storable goods that have no Mintsoft SKU match
        if not since_last_updated:
            for sku, prod_list in sku_to_odoo_products.items():
                if sku in seen_skus:
                    continue
                prod = prod_list[0]
                if prod.type == 'service':
                    continue
                existing_odoo_map = Mapping.search([
                    ('instance_id', '=', instance.id),
                    ('product_id', '=', prod.id),
                ], limit=1)
                if not existing_odoo_map:
                    Mapping.create({
                        'instance_id': instance.id,
                        'company_id': instance.company_id.id,
                        'product_id': prod.id,
                        'odoo_sku': sku,
                        'odoo_barcode': prod.barcode or '',
                        'mintsoft_sku': sku,
                        'mintsoft_name': prod.name,
                        'mapping_status': 'missing_in_mintsoft',
                        'last_sync_at': fields.Datetime.now(),
                        'last_error': _("Odoo product SKU '%s' was not found in Mintsoft.") % sku,
                    })
                    stats['missing_in_mintsoft'] += 1

        return stats

    def refresh_single_mapping(self, mapping):
        """Refresh a single kays.mintsoft.product.mapping record against Mintsoft API."""
        instance = mapping.instance_id
        client = MintsoftClient(instance)
        sku = (mapping.mintsoft_sku or mapping.odoo_sku or (mapping.product_id.default_code if mapping.product_id else '') or '').strip()

        if not sku and not mapping.mintsoft_product_id:
            mapping.write({
                'mapping_status': 'mismatch',
                'last_error': _("No SKU or Mintsoft Product ID is set on this mapping."),
                'last_sync_at': fields.Datetime.now(),
            })
            return False

        ms_prod = None
        if mapping.mintsoft_product_id and mapping.mintsoft_product_id > 0:
            ms_prod = client.request(
                'GET',
                f'/api/Product/{mapping.mintsoft_product_id}',
                operation='get_product',
                allow_404=True,
            )

        if not isinstance(ms_prod, dict) and sku:
            search_res = client.request(
                'GET',
                '/api/Product/Search',
                params={'Search': sku, 'IncludeBundles': True, 'IncludeDiscontinued': True},
                operation='search_product_by_sku',
                allow_404=True,
            )
            if isinstance(search_res, list):
                for item in search_res:
                    if isinstance(item, dict) and (item.get('SKU') or '').strip() == sku:
                        ms_prod = item
                        break

        if not isinstance(ms_prod, dict) and sku:
            prod_id = client.request(
                'GET',
                '/api/Product/LookupProductId',
                params={'SKU': sku},
                operation='lookup_product_id',
                allow_404=True,
            )
            if isinstance(prod_id, int) and prod_id > 0:
                ms_prod = client.request(
                    'GET',
                    f'/api/Product/{prod_id}',
                    operation='get_product',
                    allow_404=True,
                )

        if not isinstance(ms_prod, dict):
            mapping.write({
                'mapping_status': 'missing_in_mintsoft',
                'last_error': _("Product SKU '%s' was not found in Mintsoft.") % sku,
                'last_sync_at': fields.Datetime.now(),
            })
            return False

        ms_id = int(ms_prod.get('ID') or 0)
        ms_sku = (ms_prod.get('SKU') or sku).strip()
        barcode = (ms_prod.get('EAN') or ms_prod.get('UPC') or '').strip()

        status = 'matched' if mapping.product_id else 'missing_in_odoo'
        if mapping.product_id and mapping.product_id.default_code and mapping.product_id.default_code.strip() != ms_sku:
            # Explicit cross-SKU mapping is valid; record odoo_sku vs mintsoft_sku
            status = 'matched'

        mapping.write({
            'mintsoft_product_id': ms_id,
            'mintsoft_sku': ms_sku,
            'mintsoft_name': (ms_prod.get('Name') or ms_sku)[:250],
            'mintsoft_description': (ms_prod.get('Description') or '')[:1000],
            'mintsoft_barcode': barcode[:100],
            'mintsoft_weight': float(ms_prod.get('Weight') or 0.0),
            'mintsoft_height': float(ms_prod.get('Height') or 0.0),
            'mintsoft_width': float(ms_prod.get('Width') or 0.0),
            'mintsoft_depth': float(ms_prod.get('Depth') or 0.0),
            'mintsoft_volume': float(ms_prod.get('Volume') or 0.0),
            'is_bundle': bool(ms_prod.get('Bundle')),
            'discontinued_in_mintsoft': bool(ms_prod.get('DisCont')),
            'mapping_status': status,
            'last_error': False if status == 'matched' else _("No Odoo product linked."),
            'last_sync_at': fields.Datetime.now(),
        })
        return True

    def import_product_to_odoo(self, mapping):
        """Create or link an Odoo 19 storable product from a Mintsoft product mapping."""
        instance = mapping.instance_id
        if not instance.enable_product_import:
            raise UserError(_("Product Import is disabled on connection '%s'.") % instance.display_name)
        if mapping.product_id:
            return mapping.product_id

        sku = (mapping.mintsoft_sku or '').strip()
        if not sku:
            raise UserError(_("Cannot import product without a Mintsoft SKU."))

        Product = self.env['product.product'].with_company(instance.company_id)
        existing = Product.search([
            ('default_code', '=', sku),
            ('company_id', 'in', [False, instance.company_id.id]),
        ], limit=1)

        if existing:
            mapping.write({
                'product_id': existing.id,
                'odoo_sku': existing.default_code,
                'odoo_barcode': existing.barcode or '',
                'mapping_status': 'matched',
                'last_error': False,
                'last_sync_at': fields.Datetime.now(),
            })
            return existing

        # Create a new Odoo 19 storable product (type='consu', is_storable=True)
        prod_vals = {
            'name': mapping.mintsoft_name or sku,
            'default_code': sku,
            'type': 'consu',
            'is_storable': True,
            'weight': mapping.mintsoft_weight or 0.0,
            'volume': mapping.mintsoft_volume or 0.0,
            'description_sale': mapping.mintsoft_description or False,
            'company_id': instance.company_id.id,
        }
        if mapping.mintsoft_barcode:
            barcode_taken = Product.search([('barcode', '=', mapping.mintsoft_barcode)], limit=1)
            if not barcode_taken:
                prod_vals['barcode'] = mapping.mintsoft_barcode

        new_prod = Product.create(prod_vals)
        mapping.write({
            'product_id': new_prod.id,
            'odoo_sku': new_prod.default_code,
            'odoo_barcode': new_prod.barcode or '',
            'mapping_status': 'matched',
            'last_error': False,
            'last_sync_at': fields.Datetime.now(),
        })
        return new_prod

    def export_product_to_mintsoft(self, mapping):
        """Export an Odoo product to Mintsoft via PUT /api/Product or POST /api/Product."""
        instance = mapping.instance_id
        if not instance.active or not instance.enable_integration:
            raise UserError(_("Integration is disabled on connection '%s'.") % instance.display_name)
        if not instance.enable_product_export:
            raise UserError(_("Product Export is disabled on connection '%s'. Enable it in connection settings first.") % instance.display_name)
        if not mapping.product_id:
            raise UserError(_("Select an Odoo Product before exporting to Mintsoft."))

        prod = mapping.product_id
        sku = (mapping.mintsoft_sku or prod.default_code or '').strip()
        if not sku:
            raise UserError(_("Product '%s' must have a SKU before exporting to Mintsoft.") % prod.display_name)

        weight = float(prod.weight or mapping.mintsoft_weight or 0.01)
        payload = {
            'SKU': sku,
            'Name': (prod.name or sku)[:250],
            'Description': (prod.description_sale or prod.name or '')[:500],
            'Weight': weight,
            'Price': float(prod.lst_price or 0.0),
            'CostPrice': float(prod.standard_price or 0.0),
            'EAN': (prod.barcode or '')[:50],
            'DisCont': not prod.active,
        }
        if instance.mintsoft_client_id and instance.mintsoft_client_id > 0:
            payload['ClientId'] = instance.mintsoft_client_id

        if instance.environment == 'dry_run':
            mapping.write({
                'last_error': _("Dry Run mode: Product export payload validated locally; no write request sent."),
                'last_sync_at': fields.Datetime.now(),
            })
            return True

        client = MintsoftClient(instance)
        if mapping.mintsoft_product_id and mapping.mintsoft_product_id > 0:
            payload['ID'] = mapping.mintsoft_product_id
            res = client.request('POST', '/api/Product', json_payload=payload, operation='update_product')
            if isinstance(res, dict) and res.get('Success') is False:
                raise UserError(_("Mintsoft rejected product update: %s") % (res.get('Message') or ''))
        else:
            res = client.request('PUT', '/api/Product', json_payload=payload, operation='create_product')
            if isinstance(res, dict):
                if res.get('Success') is False:
                    raise UserError(_("Mintsoft rejected product creation: %s") % (res.get('Message') or ''))
                if res.get('ProductId'):
                    mapping.mintsoft_product_id = int(res['ProductId'])

        mapping.write({
            'mintsoft_sku': sku,
            'odoo_sku': prod.default_code or sku,
            'mapping_status': 'matched',
            'last_error': False,
            'last_sync_at': fields.Datetime.now(),
        })
        return True
