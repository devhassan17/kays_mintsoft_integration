from odoo import fields, models, _


class StockPicking(models.Model):
    _inherit = 'stock.picking'

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
