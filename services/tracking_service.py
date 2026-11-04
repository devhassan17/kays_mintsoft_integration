import logging
from odoo import fields, _

from .mintsoft_client import MintsoftClient, MintsoftAPIError

_logger = logging.getLogger(__name__)


class TrackingService:
    """Domain service for synchronizing Mintsoft Shipment Tracking Events & Couriers."""

    def __init__(self, env):
        self.env = env

    def fetch_and_sync_couriers(self, instance):
        """
        Fetch available Courier Services (/api/Courier/Services) and Service Types (/api/Courier/ServiceTypes)
        from Mintsoft and update or create kays.mintsoft.carrier.mapping records.
        """
        instance.ensure_one()
        client = MintsoftClient(instance)

        service_types = {}
        try:
            types_res = client.request('GET', '/api/Courier/ServiceTypes', operation='list_courier_service_types')
            if isinstance(types_res, list):
                for st in types_res:
                    if isinstance(st, dict) and st.get('ID'):
                        service_types[int(st['ID'])] = st.get('Name') or ''
        except MintsoftAPIError as exc:
            _logger.info("Courier/ServiceTypes fetch note: %s", exc.message)

        services_res = client.request('GET', '/api/Courier/Services', operation='list_courier_services')
        if not isinstance(services_res, list):
            return 0

        CarrierMapping = self.env['kays.mintsoft.carrier.mapping']
        DeliveryCarrier = self.env['delivery.carrier'].with_company(instance.company_id)
        count = 0

        for srv in services_res:
            if not isinstance(srv, dict):
                continue
            srv_id = int(srv.get('ID') or 0)
            srv_name = (srv.get('Name') or '').strip()
            if srv_id <= 0 or not srv_name:
                continue

            type_id = int(srv.get('CourierServiceTypeId') or 0)
            type_name = service_types.get(type_id) or srv_name.split()[0]
            tracking_url = (srv.get('TrackingURL') or '').strip()
            active_flag = bool(srv.get('ActiveB', True))

            existing = CarrierMapping.search([
                ('instance_id', '=', instance.id),
                ('mintsoft_service_id', '=', srv_id),
            ], limit=1)

            # Try matching an Odoo delivery.carrier by name if not already mapped
            matched_carrier = existing.delivery_carrier_id if existing else DeliveryCarrier.search([
                ('name', 'ilike', srv_name),
                ('company_id', 'in', [False, instance.company_id.id]),
            ], limit=1)

            vals = {
                'name': f"{type_name} - {srv_name}" if type_name and type_name not in srv_name else srv_name,
                'company_id': instance.company_id.id,
                'instance_id': instance.id,
                'delivery_carrier_id': matched_carrier.id if matched_carrier else False,
                'mintsoft_courier_type_id': type_id,
                'mintsoft_courier_name': type_name,
                'mintsoft_service_id': srv_id,
                'mintsoft_service_name': srv_name,
                'tracking_url_template': tracking_url,
                'active': active_flag,
            }
            if existing:
                existing.write(vals)
            else:
                CarrierMapping.create(vals)
            count += 1

        return count

    def sync_tracking_events(self, instance, since_last_updated=None):
        """
        Poll GET /api/Order/Shipments/TrackingEvents/List and update kays.mintsoft.tracking.event
        and linked kays.mintsoft.shipment / kays.mintsoft.order / stock.picking records.
        """
        if not instance.active or not instance.enable_integration or not instance.enable_tracking_sync:
            return 0

        client = MintsoftClient(instance)
        params = {'PageNo': 1, 'Limit': 500}
        if since_last_updated:
            params['SinceLastUpdated'] = since_last_updated

        try:
            events_data = client.request(
                'GET',
                '/api/Order/Shipments/TrackingEvents/List',
                params=params,
                operation='list_tracking_events',
                allow_404=True,
            )
        except MintsoftAPIError as exc:
            _logger.info("TrackingEvents/List returned: %s", exc.message)
            return 0

        if not isinstance(events_data, list):
            return 0

        Shipment = self.env['kays.mintsoft.shipment']
        TrackingEvent = self.env['kays.mintsoft.tracking.event']
        processed = 0

        for ev in events_data:
            if not isinstance(ev, dict):
                continue
            ev_id = int(ev.get('ID') or 0)
            ms_shipment_id = int(ev.get('OrderShipmentId') or 0)
            sh_obj = ev.get('OrderShipment') or {}
            ms_order_id = int(sh_obj.get('OrderId') or 0)
            tracking_no = (sh_obj.get('Number') or '').strip()

            shipment = Shipment.search([
                ('instance_id', '=', instance.id),
                '|',
                '&', ('mintsoft_shipment_id', '>', 0), ('mintsoft_shipment_id', '=', ms_shipment_id),
                '&', ('tracking_number', '!=', False), ('tracking_number', '=', tracking_no),
            ], limit=1)

            if not shipment and ms_order_id > 0:
                ko = self.env['kays.mintsoft.order'].search([
                    ('instance_id', '=', instance.id),
                    ('mintsoft_order_id', '=', ms_order_id),
                ], limit=1)
                if ko:
                    from .shipment_service import ShipmentService
                    shipments = ShipmentService(self.env).sync_shipments_for_order(ko)
                    shipment = shipments[:1]

            if not shipment:
                continue

            existing_ev = TrackingEvent.search([
                ('shipment_id', '=', shipment.id),
                ('mintsoft_event_id', '=', ev_id),
            ], limit=1) if ev_id > 0 else TrackingEvent

            stage_code = (ev.get('CourierTrackingStageCode') or '').strip()
            cust_desc = (ev.get('CourierCustomerDescription') or ev.get('CourierExternalDescription') or stage_code or 'Update').strip()
            ext_desc = (ev.get('CourierExternalDescription') or '').strip()
            ev_time = self._parse_dt(ev.get('CourierTimeStamp') or ev.get('LastUpdated')) or fields.Datetime.now()

            ev_vals = {
                'shipment_id': shipment.id,
                'kays_order_id': shipment.kays_order_id.id,
                'company_id': shipment.company_id.id,
                'instance_id': instance.id,
                'mintsoft_event_id': ev_id,
                'tracking_status_id': int(ev.get('TrackingStatusId') or 0),
                'stage_code': stage_code,
                'customer_description': cust_desc,
                'external_description': ext_desc,
                'courier_event_id': (ev.get('CourierTrackingEventId') or '')[:100],
                'event_timestamp': ev_time,
            }
            if existing_ev:
                existing_ev.write(ev_vals)
            else:
                TrackingEvent.create(ev_vals)

            # Check if event indicates delivery or return
            desc_lower = f"{stage_code} {cust_desc} {ext_desc}".lower()
            new_deliv_status = shipment.delivery_status
            if 'deliver' in desc_lower and 'undeliver' not in desc_lower:
                new_deliv_status = 'delivered'
                if shipment.kays_order_id.fulfilment_status != 'delivered':
                    shipment.kays_order_id.write({
                        'fulfilment_status': 'delivered',
                        'delivered_date': ev_time,
                    })
            elif 'return' in desc_lower:
                new_deliv_status = 'returned'
            elif 'transit' in desc_lower or 'hub' in desc_lower or 'out for delivery' in desc_lower:
                if new_deliv_status not in ('delivered', 'returned'):
                    new_deliv_status = 'in_transit'

            shipment.write({
                'delivery_status': new_deliv_status,
                'last_tracking_update': fields.Datetime.now(),
            })
            if shipment.picking_id:
                shipment.picking_id.write({
                    'kays_delivery_status': new_deliv_status,
                    'kays_last_tracking_update': fields.Datetime.now(),
                })
            processed += 1

        return processed

    @staticmethod
    def _parse_dt(dt_str):
        if not dt_str or not isinstance(dt_str, str):
            return False
        cleaned = dt_str.strip().replace('T', ' ').split('.')[0].split('+')[0].rstrip('Z')
        try:
            return fields.Datetime.to_datetime(cleaned[:19])
        except Exception:
            return False
