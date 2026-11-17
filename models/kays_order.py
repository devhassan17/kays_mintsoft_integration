import json
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.mintsoft_client import sanitize_payload
from ..services.order_service import OrderService

_logger = logging.getLogger(__name__)


class KaysMintsoftStatusMapping(models.Model):
    _name = 'kays.mintsoft.status.mapping'
    _description = 'Mintsoft Order Status to Odoo Fulfilment Status Mapping'
    _order = 'mintsoft_status_id, id'

    _unique_status_per_instance = models.Constraint(
        'UNIQUE(instance_id, mintsoft_status_id)',
        'Mintsoft Status ID mapping must be unique per connection!',
    )

    name = fields.Char(string='Mapping Label', compute='_compute_name', store=True)
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection (Blank = Global Default)',
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        related='instance_id.company_id',
        store=True,
    )
    mintsoft_status_id = fields.Integer(string='Mintsoft Status ID', required=True, index=True)
    mintsoft_status_name = fields.Char(string='Mintsoft Status Name', required=True)
    mintsoft_external_name = fields.Char(string='Mintsoft External Name')
    odoo_fulfilment_status = fields.Selection(
        selection=[
            ('unknown', 'Unknown'),
            ('received', 'Received'),
            ('awaiting_confirmation', 'Awaiting Confirmation'),
            ('awaiting_processing', 'Awaiting Processing'),
            ('processing', 'Processing'),
            ('picked', 'Picked'),
            ('packed', 'Packed'),
            ('partially_dispatched', 'Partially Dispatched'),
            ('dispatched', 'Dispatched'),
            ('delivered', 'Delivered'),
            ('cancelled', 'Cancelled'),
            ('on_hold', 'On Hold'),
            ('other', 'Other'),
        ],
        string='Odoo Fulfilment Status',
        required=True,
        default='received',
    )
    active = fields.Boolean(default=True)

    @api.depends('mintsoft_status_id', 'mintsoft_status_name', 'odoo_fulfilment_status')
    def _compute_name(self):
        for rec in self:
            rec.name = f"[{rec.mintsoft_status_id}] {rec.mintsoft_status_name} -> {rec.odoo_fulfilment_status}"

    @api.model
    def map_mintsoft_status(self, instance, status_id, status_name=''):
        """Resolve a Mintsoft OrderStatusId and/or status name to Odoo canonical fulfilment_status."""
        if status_id:
            if instance:
                rec = self.search([
                    ('instance_id', '=', instance.id),
                    ('mintsoft_status_id', '=', int(status_id)),
                    ('active', '=', True),
                ], limit=1)
                if rec:
                    return rec.odoo_fulfilment_status
            global_rec = self.search([
                ('instance_id', '=', False),
                ('mintsoft_status_id', '=', int(status_id)),
                ('active', '=', True),
            ], limit=1)
            if global_rec:
                return global_rec.odoo_fulfilment_status

        return self._heuristic_map_name(status_name)

    @api.model
    def _heuristic_map_name(self, status_name):
        s = (status_name or '').strip().lower()
        if not s:
            return 'unknown'
        if 'cancel' in s:
            return 'cancelled'
        if 'part' in s and ('despatch' in s or 'dispatch' in s or 'ship' in s):
            return 'partially_dispatched'
        if 'despatch' in s or 'dispatch' in s or 'shipped' in s:
            return 'dispatched'
        if 'deliver' in s:
            return 'delivered'
        if 'awaiting confirmation' in s or 'unconfirmed' in s:
            return 'awaiting_confirmation'
        if 'pack and hold' in s or 'hold' in s or 'fraud' in s or 'awaiting payment' in s or 'backorder' in s or 'back order' in s:
            return 'on_hold'
        if 'packed' in s:
            return 'packed'
        if 'picked' in s:
            return 'picked'
        if 'pick' in s or 'printing' in s or 'printed' in s or 'batch' in s or 'allocat' in s or 'process' in s:
            return 'processing'
        if 'new' in s or 'received' in s:
            return 'received'
        return 'other'

    @api.model
    def sync_from_api_payload(self, instance, statuses_list):
        if not isinstance(statuses_list, list):
            return 0
        count = 0
        for item in statuses_list:
            if not isinstance(item, dict) or not item.get('ID'):
                continue
            sid = int(item['ID'])
            sname = (item.get('Name') or f"Status {sid}").strip()
            ext_name = (item.get('ExternalName') or '').strip()
            existing = self.search([
                ('instance_id', '=', instance.id if instance else False),
                ('mintsoft_status_id', '=', sid),
            ], limit=1)
            mapped = self.map_mintsoft_status(instance, sid, sname)
            if mapped == 'unknown':
                mapped = self._heuristic_map_name(sname)
            vals = {
                'instance_id': instance.id if instance else False,
                'mintsoft_status_id': sid,
                'mintsoft_status_name': sname,
                'mintsoft_external_name': ext_name,
                'odoo_fulfilment_status': existing.odoo_fulfilment_status if existing else mapped,
            }
            if existing:
                existing.write(vals)
            else:
                self.create(vals)
            count += 1
        return count


class KaysMintsoftOrder(models.Model):
    _name = 'kays.mintsoft.order'
    _description = 'Kays Logistics Fulfilment Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'order_date desc, id desc'
    _check_company_auto = True

    _unique_idempotency_key = models.Constraint(
        'UNIQUE(idempotency_key)',
        'Idempotency key must be unique across Kays Mintsoft orders!',
    )

    name = fields.Char(
        string='Order Reference',
        required=True,
        index=True,
        tracking=True,
    )
    external_order_reference = fields.Char(
        string='External Reference',
        required=True,
        index=True,
        tracking=True,
    )
    idempotency_key = fields.Char(
        string='Idempotency Key',
        required=True,
        index=True,
        copy=False,
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Odoo Order',
        required=True,
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        index=True,
    )
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
        ondelete='restrict',
        index=True,
        check_company=True,
    )
    environment = fields.Selection(
        related='instance_id.environment',
        string='Environment',
        store=True,
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        index=True,
    )
    shipping_partner_id = fields.Many2one(
        'res.partner',
        string='Shipping Address',
    )
    mintsoft_order_id = fields.Integer(
        string='Mintsoft ID',
        index=True,
        tracking=True,
        copy=False,
    )
    mintsoft_order_number = fields.Char(
        string='Mintsoft Order Number',
        index=True,
        copy=False,
    )
    dropship_order_id = fields.Integer(string='Mintsoft Dropship Order ID', copy=False)

    # Split Order Hierarchy
    is_split_order = fields.Boolean(string='Is Split Order', default=False, index=True)
    parent_kays_order_id = fields.Many2one(
        'kays.mintsoft.order',
        string='Parent Kays Order',
        ondelete='cascade',
        index=True,
    )
    child_split_order_ids = fields.One2many(
        'kays.mintsoft.order',
        'parent_kays_order_id',
        string='Split Fulfilment Orders',
    )
    split_count = fields.Integer(compute='_compute_split_count', string='Split Orders Count')
    part_number = fields.Integer(string='Part Number', default=1)
    total_parts = fields.Integer(string='Total Parts', default=1)

    # Subscription Delivery Hierarchy
    is_subscription_delivery = fields.Boolean(
        string='Is Subscription Delivery',
        default=False,
        index=True,
    )
    subscription_cycle = fields.Integer(
        string='Subscription Cycle / Delivery #',
        default=1,
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Originating Delivery Order',
        ondelete='set null',
        index=True,
    )

    order_date = fields.Datetime(string='Order Date', index=True)
    currency_id = fields.Many2one('res.currency', string='Currency')
    order_total = fields.Monetary(string='Order Total', currency_field='currency_id')

    export_status = fields.Selection(
        selection=[
            ('not_applicable', 'Not Applicable'),
            ('pending', 'Pending'),
            ('validating', 'Validating'),
            ('queued', 'Queued'),
            ('sending', 'Sending'),
            ('sent', 'Sent'),
            ('failed', 'Failed'),
            ('unknown_outcome', 'Unknown Outcome'),
        ],
        string='Export Status',
        default='pending',
        required=True,
        index=True,
        tracking=True,
    )
    fulfilment_status = fields.Selection(
        selection=[
            ('unknown', 'Unknown'),
            ('received', 'Received'),
            ('awaiting_confirmation', 'Awaiting Confirmation'),
            ('awaiting_processing', 'Awaiting Processing'),
            ('processing', 'Processing'),
            ('picked', 'Picked'),
            ('packed', 'Packed'),
            ('partially_dispatched', 'Partially Dispatched'),
            ('dispatched', 'Dispatched'),
            ('delivered', 'Delivered'),
            ('cancelled', 'Cancelled'),
            ('on_hold', 'On Hold'),
            ('other', 'Other'),
        ],
        string='Fulfilment Status',
        default='unknown',
        required=True,
        index=True,
        tracking=True,
    )
    mintsoft_status_id = fields.Integer(string='Mintsoft Status ID', tracking=True)
    mintsoft_status_name = fields.Char(string='Raw Mintsoft Status', tracking=True)

    mintsoft_client_id = fields.Integer(string='Mintsoft Client ID')
    mintsoft_warehouse_id = fields.Integer(string='Mintsoft Warehouse ID', index=True)
    warehouse_mapping_id = fields.Many2one(
        'kays.mintsoft.warehouse.mapping',
        string='Warehouse Mapping',
    )
    warehouse_display = fields.Char(
        string='Warehouse',
        compute='_compute_warehouse_display',
        store=True,
    )
    carrier_mapping_id = fields.Many2one(
        'kays.mintsoft.carrier.mapping',
        string='Carrier Mapping',
    )
    mintsoft_courier_service_id = fields.Integer(string='Mintsoft Courier Service ID')
    mintsoft_courier_service_name = fields.Char(string='Courier', index=True)
    tracking_number = fields.Char(string='Tracking', index=True, tracking=True)
    tracking_url = fields.Char(string='Tracking URL')
    despatch_date = fields.Datetime(string='Dispatch Date', tracking=True)
    delivered_date = fields.Datetime(string='Delivered Date', tracking=True)

    last_sync_at = fields.Datetime(string='Last Sync', index=True)
    next_retry_at = fields.Datetime(string='Next Retry')
    retry_count = fields.Integer(string='Retry Count', default=0)
    last_error = fields.Text(string='Error')

    payload_preview = fields.Text(string='API Payload Preview (Sanitized)')
    response_summary = fields.Text(string='Mintsoft Response Summary (Sanitized)')

    cancellation_requested = fields.Boolean(string='Cancellation Requested', default=False, tracking=True)
    cancellation_requested_at = fields.Datetime(string='Cancellation Requested At')
    cancellation_requested_by = fields.Many2one('res.users', string='Cancellation Requested By')
    cancellation_response = fields.Text(string='Cancellation Response')

    line_ids = fields.One2many(
        'kays.mintsoft.order.line',
        'kays_order_id',
        string='Fulfilment Items',
    )
    shipment_ids = fields.One2many(
        'kays.mintsoft.shipment',
        'kays_order_id',
        string='Shipments',
    )
    tracking_event_ids = fields.One2many(
        'kays.mintsoft.tracking.event',
        'kays_order_id',
        string='Tracking Events',
    )
    sync_job_ids = fields.One2many(
        'kays.mintsoft.sync.job',
        'kays_order_id',
        string='Sync Jobs & Retry History',
    )
    log_ids = fields.One2many(
        'kays.mintsoft.log',
        'kays_order_id',
        string='API Logs',
    )
    picking_ids = fields.Many2many(
        'stock.picking',
        string='Related Odoo Delivery Orders',
        compute='_compute_picking_ids',
    )

    @api.depends('warehouse_mapping_id.mintsoft_warehouse_name', 'mintsoft_warehouse_id', 'instance_id.mintsoft_warehouse_name')
    def _compute_warehouse_display(self):
        for rec in self:
            if rec.warehouse_mapping_id and rec.warehouse_mapping_id.mintsoft_warehouse_name:
                rec.warehouse_display = f"{rec.warehouse_mapping_id.mintsoft_warehouse_name} (#{rec.mintsoft_warehouse_id})"
            elif rec.instance_id and rec.instance_id.mintsoft_warehouse_name:
                rec.warehouse_display = f"{rec.instance_id.mintsoft_warehouse_name} (#{rec.mintsoft_warehouse_id or rec.instance_id.mintsoft_warehouse_id})"
            elif rec.mintsoft_warehouse_id:
                rec.warehouse_display = f"Warehouse #{rec.mintsoft_warehouse_id}"
            else:
                rec.warehouse_display = '-'

    @api.depends('child_split_order_ids')
    def _compute_split_count(self):
        for rec in self:
            rec.split_count = len(rec.child_split_order_ids)

    @api.depends('sale_order_id.picking_ids', 'picking_id')
    def _compute_picking_ids(self):
        for rec in self:
            if rec.picking_id:
                rec.picking_ids = rec.picking_id
            elif rec.sale_order_id:
                rec.picking_ids = rec.sale_order_id.picking_ids
            else:
                rec.picking_ids = self.env['stock.picking']

    # -------------------------------------------------------------------------
    # Operational Actions (Part D & Part E)
    # -------------------------------------------------------------------------
    def action_send_to_kays(self):
        """Send selected pending/failed orders to Kays Logistics."""
        svc = OrderService(self.env)
        for rec in self:
            if rec.picking_id:
                svc.export_subscription_delivery(rec.picking_id, instance=rec.instance_id, is_manual=True)
            else:
                svc.export_sale_order(rec.sale_order_id, instance=rec.instance_id, is_manual=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Order Submission Executed'),
                'message': _('Processed %d order(s). Check Export Status for outcome.') % len(self),
                'type': 'success',
            },
        }

    def action_validate_order(self):
        """Validate order without sending to Mintsoft and refresh payload preview."""
        self.ensure_one()
        svc = OrderService(self.env)
        if self.picking_id:
            val_res = svc.validate_subscription_delivery(self.picking_id, self.instance_id, is_manual=True, external_ref=self.external_order_reference)
            payload = svc.build_mintsoft_subscription_delivery_payload(self.picking_id, self.instance_id, validation_result=val_res)
        else:
            val_res = svc.validate_sale_order(self.sale_order_id, self.instance_id, is_manual=True)
            payload = svc.build_mintsoft_order_payload(self.sale_order_id, self.instance_id, validation_result=val_res)
        self.write({
            'payload_preview': json.dumps(sanitize_payload(payload), indent=2),
            'last_error': "\n".join(val_res['errors']) if not val_res['valid'] else False,
            'last_sync_at': fields.Datetime.now(),
        })
        if val_res['valid']:
            msg = _('Order passed all 14 validation checks.')
            if val_res['warnings']:
                msg += '\n' + '\n'.join(val_res['warnings'])
            notif_type = 'success'
        else:
            msg = '\n'.join(val_res['errors'])
            notif_type = 'danger'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Order Validation Result'),
                'message': msg,
                'type': notif_type,
                'sticky': not val_res['valid'],
            },
        }

    def action_preview_payload(self):
        """Generate sanitized payload preview and open order form."""
        self.ensure_one()
        self.action_validate_order()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays Order — %s') % self.name,
            'res_model': 'kays.mintsoft.order',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_check_mintsoft_status(self):
        """Refresh order status and split information from Mintsoft."""
        svc = OrderService(self.env)
        for rec in self:
            if rec.export_status == 'unknown_outcome':
                svc.reconcile_unknown_outcome_order(rec)
            elif rec.mintsoft_order_id > 0:
                svc.sync_order_status(rec)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Status Refreshed'),
                'message': _('Synchronized status for %d order(s).') % len(self),
                'type': 'success',
            },
        }

    def action_refresh_tracking(self):
        """Refresh shipments and tracking events for the selected order(s)."""
        from ..services.shipment_service import ShipmentService
        from ..services.tracking_service import TrackingService
        ship_svc = ShipmentService(self.env)
        track_svc = TrackingService(self.env)
        instances = self.mapped('instance_id')
        for rec in self:
            if rec.mintsoft_order_id > 0:
                ship_svc.sync_shipments_for_order(rec)
        for inst in instances:
            track_svc.sync_tracking_events(inst)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Tracking Refreshed'),
                'message': _('Updated shipment and tracking information.'),
                'type': 'success',
            },
        }

    def action_retry_export(self):
        """Reconcile (if unknown outcome) and retry export for failed orders."""
        svc = OrderService(self.env)
        retried = 0
        for rec in self:
            if rec.export_status == 'sent' and rec.mintsoft_order_id > 0:
                continue
            if rec.export_status == 'unknown_outcome':
                found = svc.reconcile_unknown_outcome_order(rec)
                if found or rec.export_status == 'unknown_outcome':
                    continue
            if rec.picking_id:
                svc.export_subscription_delivery(rec.picking_id, instance=rec.instance_id, is_manual=True)
            else:
                svc.export_sale_order(rec.sale_order_id, instance=rec.instance_id, is_manual=True)
            retried += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Retry Executed'),
                'message': _('Processed retry for %d order(s).') % retried,
                'type': 'info',
            },
        }

    def action_request_cancellation(self):
        """Request order cancellation in Mintsoft after checking live status."""
        self.ensure_one()
        OrderService(self.env).request_order_cancellation(self, user=self.env.user)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Cancellation Processed'),
                'message': _('Cancellation request processed for order %s.') % self.name,
                'type': 'warning',
            },
        }

    def action_amend_order(self):
        """Push order address and item amendments to Mintsoft if in an eligible state."""
        self.ensure_one()
        OrderService(self.env).amend_order_in_mintsoft(self, update_address=True, update_items=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Order Amendment Sent'),
                'message': _('Updated order details and items in Mintsoft for %s.') % self.name,
                'type': 'success',
            },
        }

    def action_open_sale_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sales Order'),
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_sync_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('API Logs — %s') % self.name,
            'res_model': 'kays.mintsoft.log',
            'view_mode': 'list,form',
            'domain': ['|', ('kays_order_id', '=', self.id), ('sale_order_id', '=', self.sale_order_id.id)],
        }

    def action_view_split_orders(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Split Orders — %s') % self.external_order_reference,
            'res_model': 'kays.mintsoft.order',
            'view_mode': 'list,form',
            'domain': [
                '|',
                ('id', '=', self.id),
                '|',
                ('parent_kays_order_id', '=', self.id),
                '&', ('sale_order_id', '=', self.sale_order_id.id), ('id', '!=', self.id),
            ],
        }


class KaysMintsoftOrderLine(models.Model):
    _name = 'kays.mintsoft.order.line'
    _description = 'Kays Logistics Fulfilment Order Line'
    _order = 'id'

    kays_order_id = fields.Many2one(
        'kays.mintsoft.order',
        string='Kays Order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        related='kays_order_id.company_id',
        store=True,
        index=True,
    )
    sale_line_id = fields.Many2one('sale.order.line', string='Sales Order Line', ondelete='set null')
    product_id = fields.Many2one('product.product', string='Product', index=True)
    product_mapping_id = fields.Many2one('kays.mintsoft.product.mapping', string='Product Mapping')
    sku = fields.Char(string='Mintsoft SKU', required=True, index=True)
    mintsoft_product_id = fields.Integer(string='Mintsoft Product ID')
    mintsoft_order_item_id = fields.Integer(string='Mintsoft OrderItem ID')
    quantity = fields.Integer(string='Ordered Qty (Mintsoft UoM)', required=True, default=1)
    allocated_qty = fields.Integer(string='Allocated Qty', default=0)
    committed_qty = fields.Integer(string='Committed Qty', default=0)
    backorder_qty = fields.Integer(string='On Backorder Qty', default=0)
    unit_price = fields.Float(string='Unit Price (Ex VAT)', digits='Product Price')
    unit_vat = fields.Float(string='Unit VAT', digits='Product Price')
    discount = fields.Float(string='Line Discount', digits='Product Price')
