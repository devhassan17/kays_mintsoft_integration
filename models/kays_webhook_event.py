import hashlib
import json
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.mintsoft_client import sanitize_payload
from ..services.order_service import OrderService
from ..services.shipment_service import ShipmentService

_logger = logging.getLogger(__name__)


class KaysMintsoftWebhookEvent(models.Model):
    _name = 'kays.mintsoft.webhook.event'
    _description = 'Kays Logistics Incoming Mintsoft Webhook Event'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'received_at desc, id desc'
    _check_company_auto = True

    name = fields.Char(string='Event Reference', required=True, index=True)
    received_at = fields.Datetime(
        string='Timestamp',
        default=fields.Datetime.now,
        required=True,
        index=True,
    )
    processed_at = fields.Datetime(string='Processed Time', index=True)
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
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    event_type = fields.Selection(
        selection=[
            ('order_despatched', 'Order Despatched'),
            ('order_status', 'Order Status Update'),
            ('tracking_update', 'Tracking Update'),
            ('stock_update', 'Stock Update'),
            ('return_update', 'Return Update'),
            ('other', 'Other'),
        ],
        string='Event Type',
        default='order_despatched',
        required=True,
        index=True,
    )
    external_order_reference = fields.Char(string='Order Reference', index=True)
    mintsoft_order_id = fields.Integer(string='Mintsoft Order ID', index=True)
    tracking_number = fields.Char(string='Tracking Number', index=True)
    dedup_hash = fields.Char(string='Deduplication Hash', required=True, index=True)

    state = fields.Selection(
        selection=[
            ('received', 'Received'),
            ('queued', 'Queued'),
            ('processing', 'Processing'),
            ('processed', 'Processed'),
            ('duplicate', 'Duplicate (Ignored)'),
            ('ignored', 'Ignored'),
            ('failed', 'Failed'),
        ],
        string='Status',
        default='received',
        required=True,
        index=True,
        tracking=True,
    )
    kays_order_id = fields.Many2one('kays.mintsoft.order', string='Linked Kays Order', index=True, ondelete='set null')
    sync_job_id = fields.Many2one('kays.mintsoft.sync.job', string='Sync Job', index=True, ondelete='set null')
    remote_ip = fields.Char(string='Source IP')
    payload_redacted = fields.Text(string='Sanitized Event Payload')
    error_message = fields.Text(string='Error Details')
    retry_count = fields.Integer(string='Retry Count', default=0)

    @api.model
    def compute_dedup_hash(self, instance_id, raw_payload_str):
        digest_input = f"{instance_id}:{raw_payload_str or ''}".encode('utf-8')
        return hashlib.sha256(digest_input).hexdigest()

    def action_reprocess_webhook(self):
        """Manually reprocess selected webhook events with safety checks."""
        processed = 0
        for ev in self:
            ev.write({
                'state': 'queued',
                'error_message': False,
            })
            ev._process_webhook_payload(force=True)
            if ev.state == 'processed':
                processed += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Webhook Reprocessing Complete'),
                'message': _('Successfully processed %d of %d webhook event(s).') % (processed, len(self)),
                'type': 'success' if processed == len(self) else 'warning',
            },
        }

    def action_mark_ignored(self):
        self.write({'state': 'ignored'})

    def _process_webhook_payload(self, force=False):
        """
        Process a persisted webhook event asynchronously outside the raw HTTP request cycle.
        Handles out-of-order events, duplicate deliveries, and missing orders safely.
        """
        self.ensure_one()
        if self.state == 'processed' and not force:
            return True

        instance = self.instance_id.with_company(self.company_id)
        if not instance.active or not instance.enable_integration or not instance.enable_webhooks:
            self.write({
                'state': 'failed',
                'error_message': _("Webhooks or integration is disabled on connection '%s'.") % instance.display_name,
            })
            return False

        self.state = 'processing'
        try:
            payload = json.loads(self.payload_redacted or '{}')
        except ValueError:
            payload = {}

        if isinstance(payload, list) and payload:
            payload = payload[0] if isinstance(payload[0], dict) else {}

        ms_order_id = self.mintsoft_order_id or int(
            payload.get('OrderId') or payload.get('ID') or payload.get('orderId') or 0
        )
        ext_ref = (
            self.external_order_reference
            or payload.get('OrderNumber')
            or payload.get('ExternalOrderReference')
            or payload.get('SourceOrderId')
            or ''
        ).strip()
        tracking_no = (
            self.tracking_number
            or payload.get('TrackingNumber')
            or payload.get('ConsignmentNumber')
            or ''
        ).strip()

        OrderModel = self.env['kays.mintsoft.order'].with_company(self.company_id)
        kays_order = self.kays_order_id
        if not kays_order and ms_order_id > 0:
            kays_order = OrderModel.search([
                ('instance_id', '=', instance.id),
                ('mintsoft_order_id', '=', ms_order_id),
            ], limit=1)
        if not kays_order and ext_ref:
            kays_order = OrderModel.search([
                ('instance_id', '=', instance.id),
                '|',
                ('external_order_reference', '=', ext_ref),
                ('name', '=', ext_ref),
            ], limit=1)

        if not kays_order:
            self.write({
                'state': 'failed',
                'retry_count': self.retry_count + 1,
                'error_message': _(
                    "No matching Kays Order found in Odoo for Mintsoft OrderId=%(id)s / OrderNumber='%(ref)s'."
                ) % {'id': ms_order_id, 'ref': ext_ref},
            })
            return False

        self.kays_order_id = kays_order.id
        if ms_order_id > 0 and not kays_order.mintsoft_order_id:
            kays_order.mintsoft_order_id = ms_order_id

        # Update tracking / status from webhook payload
        status_id = int(payload.get('OrderStatusId') or 0)
        status_name = (payload.get('OrderStatus') or payload.get('Status') or '').strip()
        if not status_name and (self.event_type == 'order_despatched' or tracking_no or payload.get('DespatchDate')):
            status_name = 'Despatched'

        mapped_status = self.env['kays.mintsoft.status.mapping'].map_mintsoft_status(
            instance, status_id, status_name
        )

        # Out-of-order protection: do not downgrade a 'delivered' order back to 'processing' or 'received'
        status_rank = {
            'unknown': 0,
            'received': 1,
            'awaiting_confirmation': 2,
            'awaiting_processing': 3,
            'processing': 4,
            'picked': 5,
            'packed': 6,
            'partially_dispatched': 7,
            'dispatched': 8,
            'delivered': 9,
            'cancelled': 10,
            'on_hold': 4,
            'other': 1,
        }
        current_rank = status_rank.get(kays_order.fulfilment_status, 0)
        incoming_rank = status_rank.get(mapped_status, 0)

        order_vals = {'last_sync_at': fields.Datetime.now()}
        if incoming_rank >= current_rank or mapped_status == 'cancelled':
            if mapped_status != 'unknown':
                order_vals['fulfilment_status'] = mapped_status
            if status_id:
                order_vals['mintsoft_status_id'] = status_id
            if status_name:
                order_vals['mintsoft_status_name'] = status_name

        if tracking_no:
            order_vals['tracking_number'] = tracking_no
        if payload.get('TrackingURL') or payload.get('TrackingUrl'):
            order_vals['tracking_url'] = payload.get('TrackingURL') or payload.get('TrackingUrl')
        if payload.get('CourierServiceName') or payload.get('CourierService') or payload.get('CourierName'):
            order_vals['mintsoft_courier_service_name'] = (
                payload.get('CourierServiceName') or payload.get('CourierService') or payload.get('CourierName')
            )
        if payload.get('DespatchDate'):
            parsed_dt = OrderService._parse_mintsoft_datetime(payload.get('DespatchDate'))
            if parsed_dt:
                order_vals['despatch_date'] = parsed_dt

        kays_order.write(order_vals)

        # Synchronize shipment & Odoo picking if dispatch/tracking info present
        if instance.enable_shipment_sync and (
            kays_order.fulfilment_status in ('dispatched', 'partially_dispatched', 'delivered')
            or tracking_no
        ):
            ShipmentService(self.env).sync_shipments_for_order(kays_order, order_payload=payload)

        self.write({
            'state': 'processed',
            'processed_at': fields.Datetime.now(),
            'error_message': False,
        })
        return True
