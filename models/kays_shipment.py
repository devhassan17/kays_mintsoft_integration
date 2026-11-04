from odoo import api, fields, models, _


class KaysMintsoftShipment(models.Model):
    _name = 'kays.mintsoft.shipment'
    _description = 'Kays Logistics Shipment & Consignment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'shipment_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(string='Shipment Reference', required=True, index=True, tracking=True)
    mintsoft_shipment_id = fields.Integer(string='Mintsoft Shipment ID', index=True, copy=False)
    kays_order_id = fields.Many2one(
        'kays.mintsoft.order',
        string='Kays Order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Odoo Order',
        index=True,
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Odoo Delivery Order',
        index=True,
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
        index=True,
    )
    courier_type = fields.Char(string='Courier Name')
    courier_service_id = fields.Integer(string='Mintsoft Courier Service ID')
    courier_service_name = fields.Char(string='Courier Service', index=True)
    tracking_number = fields.Char(string='Tracking Number', index=True, tracking=True)
    tracking_url = fields.Char(string='Tracking URL')
    label_url = fields.Char(string='Shipping Label URL')
    parcel_numbers = fields.Char(string='Parcel Numbers')
    manifested = fields.Boolean(string='Manifested', default=False)
    shipment_date = fields.Datetime(string='Dispatch Date', index=True, tracking=True)
    delivery_status = fields.Selection(
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
        string='Delivery Status',
        default='awaiting_dispatch',
        required=True,
        index=True,
        tracking=True,
    )
    last_tracking_update = fields.Datetime(string='Last Tracking Update')
    tracking_event_ids = fields.One2many(
        'kays.mintsoft.tracking.event',
        'shipment_id',
        string='Tracking Events',
    )
    event_count = fields.Integer(compute='_compute_event_count', string='Tracking Events Count')

    @api.depends('tracking_event_ids')
    def _compute_event_count(self):
        for rec in self:
            rec.event_count = len(rec.tracking_event_ids)

    def action_refresh_tracking_events(self):
        from ..services.tracking_service import TrackingService
        svc = TrackingService(self.env)
        for inst in self.mapped('instance_id'):
            svc.sync_tracking_events(inst)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Tracking Updated'),
                'message': _('Synchronized latest courier tracking events.'),
                'type': 'success',
            },
        }


class KaysMintsoftTrackingEvent(models.Model):
    _name = 'kays.mintsoft.tracking.event'
    _description = 'Kays Logistics Shipment Tracking Event'
    _order = 'event_timestamp desc, id desc'
    _check_company_auto = True

    shipment_id = fields.Many2one(
        'kays.mintsoft.shipment',
        string='Shipment',
        required=True,
        ondelete='cascade',
        index=True,
    )
    kays_order_id = fields.Many2one(
        'kays.mintsoft.order',
        string='Kays Order',
        index=True,
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
        index=True,
    )
    tracking_number = fields.Char(related='shipment_id.tracking_number', string='Tracking Number', store=True)
    mintsoft_event_id = fields.Integer(string='Mintsoft Event ID', index=True)
    tracking_status_id = fields.Integer(string='Tracking Status ID')
    stage_code = fields.Char(string='Stage Code')
    customer_description = fields.Char(string='Status Description', required=True)
    external_description = fields.Char(string='Courier Detail')
    courier_event_id = fields.Char(string='Courier Event ID')
    event_timestamp = fields.Datetime(string='Event Timestamp', required=True, index=True)
