import logging
from odoo import fields, models, _

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    kays_enabled = fields.Boolean(
        related='company_id.kays_enabled',
        string='Kays Enabled',
        readonly=True,
    )
    kays_order_id = fields.Many2one(
        'kays.mintsoft.order',
        string='Kays Fulfilment Order',
        index=True,
        copy=False,
    )
    kays_shipment_ids = fields.One2many(
        'kays.mintsoft.shipment',
        'picking_id',
        string='Kays Shipments',
        copy=False,
    )
    mintsoft_shipment_id = fields.Integer(
        string='Mintsoft Shipment ID',
        copy=False,
    )
    kays_tracking_number = fields.Char(
        string='Kays Tracking Number',
        index=True,
        copy=False,
        tracking=True,
    )
    kays_tracking_url = fields.Char(
        string='Kays Tracking URL',
        copy=False,
    )
    kays_courier_name = fields.Char(
        string='Kays Courier',
        copy=False,
    )
    kays_dispatch_date = fields.Datetime(
        string='Kays Dispatch Date',
        copy=False,
    )
    kays_delivery_status = fields.Selection(
        selection=[
            ('awaiting_dispatch', 'Awaiting Dispatch'),
            ('processing', 'Processing'),
            ('partial_dispatch', 'Partial Dispatch'),
            ('dispatched', 'Full Dispatch'),
            ('tracking_available', 'Tracking Available'),
            ('in_transit', 'In Transit'),
            ('delivered', 'Delivered'),
            ('cancelled', 'Cancelled'),
            ('returned', 'Returned'),
        ],
        string='Kays Delivery Status',
        copy=False,
        tracking=True,
    )
    kays_last_tracking_update = fields.Datetime(
        string='Last Tracking Update',
        copy=False,
    )

    def action_kays_refresh_picking_tracking(self):
        self.ensure_one()
        if self.kays_order_id:
            return self.kays_order_id.action_refresh_tracking()
        return True

    def action_kays_send_to_kays(self):
        self.ensure_one()
        from ..services.order_service import OrderService
        svc = OrderService(self.env)
        svc.export_subscription_delivery(self, is_manual=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Kays Logistics'),
                'message': _('Delivery order sent to Kays Logistics successfully.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_confirm(self):
        res = super().action_confirm()
        if self.env.context.get('kays_skip_picking_auto_export'):
            return res
        for picking in self:
            picking._kays_check_auto_export_delivery()
        return res

    def _kays_check_auto_export_delivery(self):
        self.ensure_one()
        if self.env.context.get('kays_skip_picking_auto_export'):
            return
        if self.picking_type_id.code != 'outgoing':
            return
        if not self.move_ids or self.state in ('draft', 'cancel'):
            return
        sale_order = self.sale_id
        if not sale_order or sale_order.state != 'sale' or not sale_order.company_id.kays_enabled:
            return
        # Only auto-export from picking confirmation if the initial order was already exported to Kays
        # (initial order confirmation is handled by SaleOrder._kays_trigger_auto_export_on_confirm)
        if not sale_order.kays_order_ids.filtered(lambda o: o.export_status == 'sent'):
            return
        instance = sale_order.kays_instance_id or sale_order.company_id._get_default_kays_instance(sale_order=sale_order)
        if not instance or not instance.active or not instance.enable_integration or not instance.enable_subscription_export:
            return
        if self.kays_order_id and self.kays_order_id.export_status == 'sent':
            return
        from ..services.order_service import OrderService
        svc = OrderService(self.env.with_context(kays_skip_picking_auto_export=True))
        pay_ok, _reason = svc.check_payment_eligibility(sale_order, instance)
        if pay_ok:
            try:
                with self.env.cr.savepoint():
                    svc.export_subscription_delivery(
                        self.with_context(kays_skip_picking_auto_export=True),
                        instance=instance,
                        is_manual=False,
                    )
            except Exception as exc:
                _logger.warning("Auto-export of delivery %s to Kays skipped/failed: %s", self.name, exc)


class StockMove(models.Model):
    _inherit = 'stock.move'

    kays_mintsoft_order_item_id = fields.Integer(
        string='Mintsoft OrderItem ID',
        copy=False,
    )


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    kays_warehouse_mapping_ids = fields.One2many(
        'kays.mintsoft.warehouse.mapping',
        'odoo_warehouse_id',
        string='Kays Warehouse Mappings',
    )


class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    kays_carrier_mapping_ids = fields.One2many(
        'kays.mintsoft.carrier.mapping',
        'delivery_carrier_id',
        string='Kays Courier Mappings',
    )
