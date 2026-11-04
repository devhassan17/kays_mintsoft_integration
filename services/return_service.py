import json
import logging
from odoo import fields, _
from odoo.exceptions import UserError

from .mintsoft_client import MintsoftClient, MintsoftAPIError, sanitize_payload

_logger = logging.getLogger(__name__)


class ReturnService:
    """Domain service for Mintsoft Return / RMA creation and status synchronization."""

    def __init__(self, env):
        self.env = env

    def create_return_in_mintsoft(self, kays_return):
        """
        Submit a kays.mintsoft.return to Mintsoft via:
        1. POST /api/Return/CreateReturn/{OrderId}?WarehouseId=<wid>&Reference=<ref>
           followed by POST /api/Return/{id}/AddItem for each line, OR
        2. POST /api/Return/CreateExternalReturn if no Mintsoft OrderId is linked.
        Never issues automatic refunds unless separately approved!
        """
        kays_return.ensure_one()
        instance = kays_return.instance_id
        if not instance.active or not instance.enable_integration:
            raise UserError(_("Integration is disabled on connection '%s'.") % instance.display_name)
        if not instance.enable_returns_sync:
            raise UserError(_("Returns Sync is disabled on connection '%s'.") % instance.display_name)
        if not kays_return.line_ids:
            raise UserError(_("Add at least one return line before submitting to Mintsoft."))

        if instance.environment == 'dry_run':
            kays_return.write({
                'state': 'submitted',
                'last_error': _("Dry Run mode: Return validated locally; no write request sent to Mintsoft."),
                'last_sync_at': fields.Datetime.now(),
            })
            return True

        client = MintsoftClient(instance)
        ms_order_id = kays_return.mintsoft_order_id or (
            kays_return.kays_order_id.mintsoft_order_id if kays_return.kays_order_id else 0
        )
        warehouse_id = instance.mintsoft_warehouse_id or 3

        if ms_order_id and ms_order_id > 0:
            # Step 1: Create return header against OrderId
            res = client.request(
                'POST',
                f'/api/Return/CreateReturn/{ms_order_id}',
                params={
                    'WarehouseId': warehouse_id,
                    'Reference': kays_return.name,
                },
                operation='create_return_for_order',
                sale_order=kays_return.sale_order_id,
                kays_order=kays_return.kays_order_id,
                mintsoft_order_id=ms_order_id,
            )
            if not isinstance(res, dict) or not res.get('Success', True) or not res.get('ID'):
                msg = res.get('Message') if isinstance(res, dict) else str(res)
                kays_return.write({
                    'state': 'failed',
                    'last_error': msg or _("Failed to create return in Mintsoft."),
                    'last_sync_at': fields.Datetime.now(),
                })
                raise UserError(_("Mintsoft return creation failed: %s") % (msg or _('Unknown error')))

            ms_return_id = int(res['ID'])
            kays_return.mintsoft_return_id = ms_return_id

            # Step 2: Add return items via POST /api/Return/{id}/AddItem
            for line in kays_return.line_ids:
                prod_id = line.mintsoft_product_id or (
                    line.product_mapping_id.mintsoft_product_id if line.product_mapping_id else 0
                )
                item_payload = {
                    'ProductId': prod_id,
                    'Quantity': int(line.quantity or 1),
                    'ReturnReasonId': int(line.mintsoft_reason_id or kays_return.mintsoft_reason_id or 1),
                    'Action': line.requested_action or 'Refund',
                    'Comments': (line.notes or kays_return.reason or '')[:250],
                }
                client.request(
                    'POST',
                    f'/api/Return/{ms_return_id}/AddItem',
                    json_payload=item_payload,
                    operation='add_return_item',
                    sale_order=kays_return.sale_order_id,
                    kays_order=kays_return.kays_order_id,
                    mintsoft_order_id=ms_order_id,
                )
        else:
            # External Return (not linked to a Mintsoft OrderId)
            items_payload = []
            for line in kays_return.line_ids:
                items_payload.append({
                    'ProductId': line.mintsoft_product_id or 0,
                    'SKU': line.sku or line.product_id.default_code or '',
                    'Quantity': int(line.quantity or 1),
                    'ReturnReasonId': int(line.mintsoft_reason_id or kays_return.mintsoft_reason_id or 1),
                    'Action': line.requested_action or 'Refund',
                    'Comments': (line.notes or kays_return.reason or '')[:250],
                })
            ext_payload = {
                'ClientId': instance.mintsoft_client_id or 0,
                'WarehouseId': warehouse_id,
                'Reference': kays_return.name,
                'ReturnItems': items_payload,
            }
            res = client.request(
                'POST',
                '/api/Return/CreateExternalReturn',
                json_payload=ext_payload,
                operation='create_external_return',
                sale_order=kays_return.sale_order_id,
            )
            if not isinstance(res, dict) or not res.get('Success', True) or not res.get('ID'):
                msg = res.get('Message') if isinstance(res, dict) else str(res)
                kays_return.write({
                    'state': 'failed',
                    'last_error': msg or _("Failed to create external return in Mintsoft."),
                    'last_sync_at': fields.Datetime.now(),
                })
                raise UserError(_("Mintsoft external return creation failed: %s") % (msg or _('Unknown error')))
            kays_return.mintsoft_return_id = int(res['ID'])

        kays_return.write({
            'state': 'submitted',
            'last_error': False,
            'last_sync_at': fields.Datetime.now(),
        })
        return True

    def sync_return_status(self, kays_return):
        """Fetch latest return details and items from GET /api/Return/{id} and GET /api/Return/{id}/Items."""
        kays_return.ensure_one()
        instance = kays_return.instance_id
        if not instance.active or not instance.enable_integration or not instance.enable_returns_sync:
            return False
        if not kays_return.mintsoft_return_id or kays_return.mintsoft_return_id <= 0:
            return False

        client = MintsoftClient(instance)
        ret_data = client.request(
            'GET',
            f'/api/Return/{kays_return.mintsoft_return_id}',
            operation='get_return',
            sale_order=kays_return.sale_order_id,
            kays_order=kays_return.kays_order_id,
            allow_404=True,
        )
        if not isinstance(ret_data, dict):
            return False

        items_data = ret_data.get('ReturnItems')
        if not isinstance(items_data, list):
            items_data = client.request(
                'GET',
                f'/api/Return/{kays_return.mintsoft_return_id}/Items',
                operation='get_return_items',
                sale_order=kays_return.sale_order_id,
                kays_order=kays_return.kays_order_id,
                allow_404=True,
            ) or []

        confirmed = bool(ret_data.get('Confirmed'))
        total_received = 0
        any_restocked = False
        any_scrapped = False

        for r_item in items_data:
            if not isinstance(r_item, dict):
                continue
            sku = (r_item.get('SKU') or '').strip()
            ms_prod_id = int(r_item.get('ProductId') or 0)
            qty = int(r_item.get('Quantity') or 0)
            returned_to_stock = bool(r_item.get('ReturnedToStock'))
            stock_action = (r_item.get('StockAction') or '').lower()

            line = kays_return.line_ids.filtered(
                lambda l: (sku and l.sku == sku) or (ms_prod_id and l.mintsoft_product_id == ms_prod_id)
            )[:1]
            if line:
                line.write({
                    'received_quantity': qty if confirmed or returned_to_stock else line.received_quantity,
                    'returned_to_stock': returned_to_stock,
                    'stock_action': r_item.get('StockAction') or '',
                })
            if confirmed or returned_to_stock:
                total_received += qty
            if returned_to_stock or 'stock' in stock_action or 'good' in stock_action:
                any_restocked = True
            elif 'scrap' in stock_action or 'damage' in stock_action:
                any_scrapped = True

        new_state = kays_return.state
        if confirmed:
            new_state = 'received' if total_received > 0 else 'confirmed'

        inv_outcome = kays_return.inventory_outcome
        if any_restocked:
            inv_outcome = 'restocked'
        elif any_scrapped:
            inv_outcome = 'scrapped'

        kays_return.write({
            'state': new_state,
            'inventory_outcome': inv_outcome,
            'last_error': False,
            'last_sync_at': fields.Datetime.now(),
        })
        return True

    def poll_returns_list(self, instance, since_last_updated=None):
        """Poll GET /api/Return/List and synchronize or import returns for this connection."""
        if not instance.active or not instance.enable_integration or not instance.enable_returns_sync:
            return 0

        client = MintsoftClient(instance)
        params = {'PageNo': 1, 'Limit': 100, 'IncludeReturnItems': True}
        if instance.mintsoft_client_id and instance.mintsoft_client_id > 0:
            params['ClientId'] = instance.mintsoft_client_id
        if instance.mintsoft_warehouse_id and instance.mintsoft_warehouse_id > 0:
            params['WarehouseId'] = instance.mintsoft_warehouse_id
        if since_last_updated:
            params['SinceLastUpdated'] = since_last_updated

        returns_list = client.request('GET', '/api/Return/List', params=params, operation='list_returns', allow_404=True)
        if not isinstance(returns_list, list):
            return 0

        ReturnModel = self.env['kays.mintsoft.return']
        synced = 0
        for ret_obj in returns_list:
            if not isinstance(ret_obj, dict) or not ret_obj.get('ID'):
                continue
            ms_ret_id = int(ret_obj['ID'])
            existing = ReturnModel.search([
                ('instance_id', '=', instance.id),
                ('mintsoft_return_id', '=', ms_ret_id),
            ], limit=1)
            if existing:
                self.sync_return_status(existing)
                synced += 1
                continue

            ms_order_id = int(ret_obj.get('OrderId') or 0)
            kays_order = self.env['kays.mintsoft.order'].search([
                ('instance_id', '=', instance.id),
                ('mintsoft_order_id', '=', ms_order_id),
            ], limit=1) if ms_order_id > 0 else self.env['kays.mintsoft.order']

            new_ret = ReturnModel.create({
                'name': ret_obj.get('Reference') or f"MS-RET-{ms_ret_id}",
                'mintsoft_return_id': ms_ret_id,
                'company_id': instance.company_id.id,
                'instance_id': instance.id,
                'kays_order_id': kays_order.id if kays_order else False,
                'sale_order_id': kays_order.sale_order_id.id if kays_order else False,
                'mintsoft_order_id': ms_order_id,
                'state': 'received' if ret_obj.get('Confirmed') else 'submitted',
                'last_sync_at': fields.Datetime.now(),
            })
            for r_item in (ret_obj.get('ReturnItems') or []):
                if not isinstance(r_item, dict):
                    continue
                sku = (r_item.get('SKU') or '').strip()
                mapping = self.env['kays.mintsoft.product.mapping'].search([
                    ('instance_id', '=', instance.id),
                    ('mintsoft_sku', '=', sku),
                ], limit=1) if sku else self.env['kays.mintsoft.product.mapping']
                self.env['kays.mintsoft.return.line'].create({
                    'return_id': new_ret.id,
                    'product_mapping_id': mapping.id if mapping else False,
                    'product_id': mapping.product_id.id if (mapping and mapping.product_id) else False,
                    'sku': sku,
                    'mintsoft_product_id': int(r_item.get('ProductId') or 0),
                    'quantity': float(r_item.get('Quantity') or 1.0),
                    'received_quantity': float(r_item.get('Quantity') or 0.0) if ret_obj.get('Confirmed') else 0.0,
                    'returned_to_stock': bool(r_item.get('ReturnedToStock')),
                    'stock_action': r_item.get('StockAction') or '',
                    'notes': r_item.get('Comments') or '',
                })
            synced += 1
        return synced
