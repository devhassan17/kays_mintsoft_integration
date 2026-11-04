import hmac
import json
import logging
from datetime import timedelta

from odoo import fields, http, _
from odoo.http import request, Response

from ..services.mintsoft_client import sanitize_payload

_logger = logging.getLogger(__name__)


class KaysMintsoftWebhookController(http.Controller):
    """
    HTTPS Webhook Receiver for Mintsoft Order / ConnectAction / Fulfilment Callbacks.
    Route: /kays/mintsoft/webhook/<string:connection_identifier>
    """

    @http.route(
        '/kays/mintsoft/webhook/<string:connection_identifier>',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def receive_mintsoft_webhook(self, connection_identifier, **kwargs):
        env = request.env
        Instance = env['kays.mintsoft.instance'].sudo()

        # 1. Identify connection by non-guessable random identifier
        instance = Instance.search([
            ('webhook_identifier', '=', connection_identifier),
            ('active', '=', True),
        ], limit=1)
        if not instance or not instance.company_id.kays_enabled:
            return self._json_response(404, {'status': 'error', 'message': 'Webhook endpoint not found or inactive.'})

        if not instance.enable_integration or not instance.enable_webhooks:
            return self._json_response(403, {'status': 'error', 'message': 'Webhooks are disabled for this connection.'})

        # 2. Enforce request size limit
        max_bytes = max(4096, instance.webhook_max_payload_bytes or 262144)
        raw_bytes = request.httprequest.get_data(cache=False, as_text=False) or b''
        if len(raw_bytes) > max_bytes:
            return self._json_response(413, {'status': 'error', 'message': 'Payload exceeds maximum allowed size.'})

        # 3. Validate token authenticity when supplied via query string or header
        expected_secret = (instance.webhook_secret or '').strip()
        supplied_token = (
            kwargs.get('token')
            or request.httprequest.headers.get('X-Kays-Webhook-Token')
            or request.httprequest.headers.get('X-Mintsoft-Token')
            or ''
        ).strip()
        if supplied_token and expected_secret:
            if not hmac.compare_digest(supplied_token, expected_secret):
                return self._json_response(401, {'status': 'error', 'message': 'Invalid webhook verification token.'})

        # 4. Enforce per-connection rate limit (events in the last 60 seconds)
        WebhookEvent = env['kays.mintsoft.webhook.event'].sudo()
        rate_limit = max(10, instance.webhook_rate_limit_per_min or 120)
        one_min_ago = fields.Datetime.now() - timedelta(seconds=60)
        recent_count = WebhookEvent.search_count([
            ('instance_id', '=', instance.id),
            ('received_at', '>=', one_min_ago),
        ])
        if recent_count >= rate_limit:
            return self._json_response(
                429,
                {'status': 'error', 'message': 'Webhook rate limit exceeded.'},
                headers=[('Retry-After', '60')],
            )

        # 5. Parse and validate JSON payload structure
        raw_text = raw_bytes.decode('utf-8', errors='replace').strip()
        if not raw_text:
            return self._json_response(400, {'status': 'error', 'message': 'Empty webhook payload.'})

        try:
            payload = json.loads(raw_text)
        except ValueError:
            return self._json_response(400, {'status': 'error', 'message': 'Invalid JSON payload.'})

        if not isinstance(payload, (dict, list)):
            return self._json_response(400, {'status': 'error', 'message': 'Webhook payload must be a JSON object or array.'})

        primary_obj = payload[0] if (isinstance(payload, list) and payload and isinstance(payload[0], dict)) else (
            payload if isinstance(payload, dict) else {}
        )

        # 6. Deduplication check via SHA-256 hash
        dedup_hash = WebhookEvent.compute_dedup_hash(instance.id, raw_text)
        existing_dup = WebhookEvent.search([
            ('instance_id', '=', instance.id),
            ('dedup_hash', '=', dedup_hash),
            ('state', 'in', ['received', 'queued', 'processing', 'processed']),
        ], limit=1)

        ms_order_id = int(primary_obj.get('OrderId') or primary_obj.get('ID') or primary_obj.get('orderId') or 0)
        ext_ref = str(
            primary_obj.get('OrderNumber')
            or primary_obj.get('ExternalOrderReference')
            or primary_obj.get('SourceOrderId')
            or ''
        ).strip()
        tracking_no = str(
            primary_obj.get('TrackingNumber')
            or primary_obj.get('ConsignmentNumber')
            or ''
        ).strip()
        event_type = self._classify_event_type(primary_obj, tracking_no)
        sanitized_json = json.dumps(sanitize_payload(payload), indent=2, default=str)
        remote_ip = request.httprequest.remote_addr or ''

        if existing_dup:
            dup_event = WebhookEvent.create({
                'name': f"WH-DUP-{ext_ref or ms_order_id or fields.Datetime.now().strftime('%H%M%S')}",
                'received_at': fields.Datetime.now(),
                'processed_at': fields.Datetime.now(),
                'company_id': instance.company_id.id,
                'instance_id': instance.id,
                'event_type': event_type,
                'external_order_reference': ext_ref,
                'mintsoft_order_id': ms_order_id,
                'tracking_number': tracking_no,
                'dedup_hash': dedup_hash,
                'state': 'duplicate',
                'kays_order_id': existing_dup.kays_order_id.id if existing_dup.kays_order_id else False,
                'remote_ip': remote_ip,
                'payload_redacted': sanitized_json,
            })
            return self._json_response(200, {
                'status': 'duplicate_ignored',
                'event_id': dup_event.id,
            })

        # 7. Persist webhook event and queue a durable sync job for asynchronous processing
        event = WebhookEvent.create({
            'name': f"WH-{event_type.upper()}-{ext_ref or ms_order_id or fields.Datetime.now().strftime('%H%M%S')}",
            'received_at': fields.Datetime.now(),
            'company_id': instance.company_id.id,
            'instance_id': instance.id,
            'event_type': event_type,
            'external_order_reference': ext_ref,
            'mintsoft_order_id': ms_order_id,
            'tracking_number': tracking_no,
            'dedup_hash': dedup_hash,
            'state': 'queued',
            'remote_ip': remote_ip,
            'payload_redacted': sanitized_json,
        })

        job = env['kays.mintsoft.sync.job'].sudo().enqueue_job(
            instance=instance,
            job_type='webhook_process',
            webhook_event=event,
            payload={'webhook_event_id': event.id},
            priority=20,
        )
        event.sync_job_id = job.id

        return self._json_response(200, {
            'status': 'queued',
            'event_id': event.id,
            'job_id': job.id,
        })

    @staticmethod
    def _classify_event_type(obj, tracking_no):
        status_str = str(obj.get('OrderStatus') or obj.get('Status') or obj.get('EventType') or '').lower()
        if 'despatch' in status_str or 'dispatch' in status_str or obj.get('DespatchDate'):
            return 'order_despatched'
        if tracking_no or 'track' in status_str:
            return 'tracking_update'
        if 'return' in status_str or obj.get('ReturnItems'):
            return 'return_update'
        if 'stock' in status_str or obj.get('StockLevel') is not None:
            return 'stock_update'
        if obj.get('OrderId') or obj.get('OrderNumber'):
            return 'order_status'
        return 'other'

    @staticmethod
    def _json_response(status_code, body_dict, headers=None):
        resp_headers = [('Content-Type', 'application/json')]
        if headers:
            resp_headers.extend(headers)
        return Response(
            json.dumps(body_dict),
            status=status_code,
            headers=resp_headers,
        )
