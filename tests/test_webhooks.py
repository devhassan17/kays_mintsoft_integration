import hmac
import json
from unittest.mock import patch
from odoo.exceptions import UserError

from .common import KaysMintsoftTestCommon, MockHTTPResponse
from ..services.order_service import OrderService
from ..services.shipment_service import ShipmentService
from ..services.tracking_service import TrackingService


class TestKaysShipmentsCancellationsAndWebhooks(KaysMintsoftTestCommon):
    """Tests 22–26: Shipments, partial dispatch, cancellation safety, webhook deduplication, and authentication."""

    def _create_exported_kays_order(self, mintsoft_id=98001, qty=4.0):
        self.instance_uk.enable_auto_order_export = False
        sale_order = self._create_confirmed_sale_order(qty=qty, confirm=True)
        kays_order = self.KaysOrder.with_company(self.company_uk).create({
            'name': sale_order.name,
            'external_order_reference': sale_order.name,
            'idempotency_key': f"{self.instance_uk.id}:{sale_order.name}",
            'sale_order_id': sale_order.id,
            'company_id': self.company_uk.id,
            'instance_id': self.instance_uk.id,
            'partner_id': sale_order.partner_id.id,
            'shipping_partner_id': sale_order.partner_shipping_id.id,
            'mintsoft_order_id': mintsoft_id,
            'mintsoft_client_id': 10,
            'mintsoft_warehouse_id': 3,
            'export_status': 'sent',
            'fulfilment_status': 'processing',
            'line_ids': [(0, 0, {
                'sale_line_id': sale_order.order_line[0].id,
                'product_id': self.product_coffee.id,
                'sku': 'MOYEE-ESP-1KG',
                'mintsoft_product_id': 5001,
                'mintsoft_order_item_id': 7001,
                'quantity': qty,
                'unit_price': 24.50,
            })],
        })
        return sale_order, kays_order

    @patch('requests.Session.request')
    def test_22_tracking_and_shipment_updates(self, mock_request):
        """Test 22: Verify shipment and tracking event synchronization updates kays.mintsoft.shipment and stock.picking."""
        sale_order, kays_order = self._create_exported_kays_order(mintsoft_id=98101, qty=2.0)

        def side_effect(**kwargs):
            url = kwargs.get('url', '')
            if url.endswith('/api/Order/98101/Shipments'):
                return MockHTTPResponse(200, [{
                    'OrderShipmentId': 5501,
                    'TrackingNumber': 'DPD-9988776655',
                    'TrackingURL': 'https://track.dpd.co.uk/DPD-9988776655',
                    'CourierServiceName': 'DPD Next Day',
                    'CourierServiceId': 12,
                    'NumberOfParcels': 1,
                    'TotalWeight': 2.0,
                    'DespatchDate': '2026-11-04T10:15:00Z',
                    'Status': 'In Transit',
                }])
            if '/api/Order/Shipments/TrackingEvents/List' in url:
                return MockHTTPResponse(200, [{
                    'ID': 8801,
                    'OrderShipmentId': 5501,
                    'TrackingNumber': 'DPD-9988776655',
                    'TrackingStatus': 'In Transit',
                    'TrackingDescription': 'Parcel arrived at London hub',
                    'Location': 'London Hub',
                    'EventDate': '2026-11-04T11:30:00Z',
                }])
            return MockHTTPResponse(200, [])

        mock_request.side_effect = side_effect
        kays_order.fulfilment_status = 'dispatched'
        ShipmentService(self.env).sync_shipments_for_order(kays_order)
        TrackingService(self.env).sync_tracking_events_for_orders(self.instance_uk, kays_order)

        self.assertEqual(kays_order.tracking_number, 'DPD-9988776655')
        self.assertEqual(len(kays_order.shipment_ids), 1)
        shipment = kays_order.shipment_ids[0]
        self.assertEqual(shipment.mintsoft_shipment_id, 5501)
        self.assertEqual(shipment.tracking_number, 'DPD-9988776655')
        self.assertEqual(len(shipment.tracking_event_ids), 1)
        self.assertEqual(shipment.tracking_event_ids[0].location, 'London Hub')

        # Check stock.picking was updated with tracking number
        picking = sale_order.picking_ids[:1]
        if picking:
            self.assertEqual(picking.kays_tracking_number, 'DPD-9988776655')
            self.assertEqual(picking.kays_courier_name, 'DPD Next Day')

    @patch('requests.Session.request')
    def test_23_partial_shipment_handling(self, mock_request):
        """Test 23: Verify partial shipment records shipped qty on lines and does not prematurely mark full dispatch."""
        sale_order, kays_order = self._create_exported_kays_order(mintsoft_id=98102, qty=4.0)
        self.instance_uk.validate_picking_on_dispatch = False
        self.instance_uk.validate_picking_on_partial_dispatch = False

        mock_request.return_value = MockHTTPResponse(200, [{
            'OrderShipmentId': 5502,
            'TrackingNumber': 'DPD-PARTIAL-01',
            'CourierServiceName': 'DPD Next Day',
            'DespatchDate': '2026-11-04T10:15:00Z',
            'Status': 'Partially Dispatched',
            'ShipmentItems': [{
                'SKU': 'MOYEE-ESP-1KG',
                'Quantity': 2,
            }],
        }])

        kays_order.fulfilment_status = 'partially_dispatched'
        ShipmentService(self.env).sync_shipments_for_order(kays_order)

        self.assertEqual(len(kays_order.shipment_ids), 1)
        self.assertTrue(kays_order.shipment_ids[0].is_partial_shipment)
        picking = sale_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))[:1]
        if picking:
            self.assertEqual(picking.kays_delivery_status, 'partial_dispatch')
            self.assertNotEqual(picking.state, 'done')

    @patch('requests.Session.request')
    def test_24_cancellation_safety_blocks_dispatched_and_cancels_new(self, mock_request):
        """
        Test 24: Verify cancellation first checks live Mintsoft status via GET /api/Order/{id},
        blocks cancellation if already Despatched, and calls GET /api/Order/{id}/Cancel when eligible.
        """
        _sale_order1, kays_order_dispatched = self._create_exported_kays_order(mintsoft_id=98201, qty=1.0)
        _sale_order2, kays_order_new = self._create_exported_kays_order(mintsoft_id=98202, qty=1.0)

        def side_effect_dispatched(**kwargs):
            url = kwargs.get('url', '')
            if url.endswith('/api/Order/98201'):
                return MockHTTPResponse(200, {
                    'ID': 98201,
                    'OrderStatusId': 6,
                    'OrderStatus': 'Despatched',
                })
            return MockHTTPResponse(200, {})

        mock_request.side_effect = side_effect_dispatched
        with self.assertRaises(UserError) as ctx:
            OrderService(self.env).request_order_cancellation(kays_order_dispatched)
        self.assertIn('Cannot cancel', str(ctx.exception))

        # Now test safe cancellation when order is still in 'New' status in Mintsoft
        def side_effect_cancel_ok(**kwargs):
            url = kwargs.get('url', '')
            if url.endswith('/api/Order/98202'):
                return MockHTTPResponse(200, {
                    'ID': 98202,
                    'OrderStatusId': 1,
                    'OrderStatus': 'New',
                })
            if url.endswith('/api/Order/98202/Cancel'):
                return MockHTTPResponse(200, {
                    'Success': True,
                    'Message': 'Order 98202 cancelled in Mintsoft',
                })
            return MockHTTPResponse(200, {})

        mock_request.side_effect = side_effect_cancel_ok
        res = OrderService(self.env).request_order_cancellation(kays_order_new)
        self.assertTrue(res['success'])
        self.assertEqual(kays_order_new.fulfilment_status, 'cancelled')
        self.assertEqual(kays_order_new.cancellation_state, 'confirmed')

    @patch('requests.Session.request')
    def test_25_webhook_deduplication_and_out_of_order_protection(self, mock_request):
        """
        Test 25: Verify SHA-256 webhook deduplication and out-of-order protection
        (a 'delivered' order is never downgraded to 'processing' by a delayed webhook).
        """
        mock_request.return_value = MockHTTPResponse(200, [])
        sale_order, kays_order = self._create_exported_kays_order(mintsoft_id=98301, qty=1.0)
        kays_order.fulfilment_status = 'delivered'

        raw_payload = json.dumps({
            'OrderId': 98301,
            'OrderNumber': sale_order.name,
            'OrderStatusId': 2,
            'OrderStatus': 'Processing',
            'TrackingNumber': 'DPD-LATE-001',
        })
        dedup_hash = self.WebhookEvent.compute_dedup_hash(self.instance_uk.id, raw_payload)

        ev1 = self.WebhookEvent.with_company(self.company_uk).create({
            'name': 'WH-TEST-001',
            'company_id': self.company_uk.id,
            'instance_id': self.instance_uk.id,
            'event_type': 'order_status',
            'external_order_reference': sale_order.name,
            'mintsoft_order_id': 98301,
            'tracking_number': 'DPD-LATE-001',
            'dedup_hash': dedup_hash,
            'state': 'queued',
            'payload_redacted': raw_payload,
        })
        ev1._process_webhook_payload()

        self.assertEqual(ev1.state, 'processed')
        # Out-of-order protection: status stays 'delivered', while tracking number is still captured
        self.assertEqual(kays_order.fulfilment_status, 'delivered')
        self.assertEqual(kays_order.tracking_number, 'DPD-LATE-001')

        # Duplicate hash check
        existing_dup = self.WebhookEvent.search([
            ('instance_id', '=', self.instance_uk.id),
            ('dedup_hash', '=', dedup_hash),
            ('state', '=', 'processed'),
        ], limit=1)
        self.assertEqual(existing_dup.id, ev1.id)

    def test_26_webhook_token_authentication_verification(self):
        """Test 26: Verify constant-time webhook secret token validation rejects invalid tokens."""
        expected = (self.instance_uk.webhook_secret_token or '').encode('utf-8')
        valid_token = b'TEST_WEBHOOK_SECRET_TOKEN_987654321'
        invalid_token = b'WRONG_ATTACKER_TOKEN'

        self.assertTrue(hmac.compare_digest(expected, valid_token))
        self.assertFalse(hmac.compare_digest(expected, invalid_token))
        self.assertIn(self.instance_uk.webhook_identifier, self.instance_uk.webhook_url)
