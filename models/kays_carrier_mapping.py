from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.mintsoft_client import MintsoftClient, MintsoftAPIError
from ..services.tracking_service import TrackingService


class KaysMintsoftCarrierMapping(models.Model):
    _name = 'kays.mintsoft.carrier.mapping'
    _description = 'Kays Logistics Courier & Shipping Service Mapping'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'company_id, instance_id, mintsoft_service_name'
    _check_company_auto = True

    name = fields.Char(string='Mapping Name', required=True, tracking=True)
    active = fields.Boolean(string='Active', default=True, tracking=True)
    company_id = fields.Many2one(
        'res.company',
        string='Odoo Company',
        required=True,
        default=lambda self: self.env.company,
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
    delivery_carrier_id = fields.Many2one(
        'delivery.carrier',
        string='Odoo Delivery Carrier',
        index=True,
        tracking=True,
    )
    mintsoft_courier_type_id = fields.Integer(string='Mintsoft Courier ID', tracking=True)
    mintsoft_courier_name = fields.Char(string='Mintsoft Courier Name', tracking=True)
    mintsoft_service_id = fields.Integer(
        string='Mintsoft Service ID',
        required=True,
        index=True,
        tracking=True,
    )
    mintsoft_service_name = fields.Char(
        string='Mintsoft Service Name',
        required=True,
        tracking=True,
    )
    tracking_url_template = fields.Char(string='Tracking URL Template')
    region_policy = fields.Selection(
        selection=[
            ('all', 'All Destinations'),
            ('uk_mainland', 'United Kingdom (Mainland)'),
            ('northern_ireland', 'Northern Ireland (BT Postcodes)'),
            ('ireland', 'Republic of Ireland (IE)'),
            ('international', 'International / Rest of World'),
        ],
        string='Country / Region Policy',
        default='all',
        required=True,
        tracking=True,
    )
    country_ids = fields.Many2many(
        'res.country',
        'kays_carrier_mapping_country_rel',
        'mapping_id',
        'country_id',
        string='Specific Countries',
    )
    postcode_prefixes = fields.Char(
        string='Postcode Prefixes',
        help='Optional comma-separated postcode prefixes (e.g., BT for Northern Ireland).',
    )
    fallback_policy = fields.Selection(
        selection=[
            ('block', 'Block Order Export if Invalid'),
            ('use_instance_default', 'Use Connection Default Courier Service'),
            ('use_fallback_service', 'Use Specified Fallback Service ID'),
        ],
        string='Fallback Policy',
        default='block',
        required=True,
    )
    fallback_service_id = fields.Integer(string='Fallback Mintsoft Service ID')
    fallback_service_name = fields.Char(string='Fallback Mintsoft Service Name')
    last_verified_at = fields.Datetime(string='Last Verified At', readonly=True)

    # -------------------------------------------------------------------------
    # Courier Mapping Action Buttons (Part I - Section 14)
    # -------------------------------------------------------------------------
    def action_fetch_couriers_from_mintsoft(self):
        """Fetch Courier Service Types and Services from Mintsoft."""
        svc = TrackingService(self.env)
        total = 0
        for inst in self.mapped('instance_id'):
            total += svc.fetch_and_sync_couriers(inst)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Couriers Synchronized'),
                'message': _('Synchronized %d courier service(s) from Mintsoft.') % total,
                'type': 'success',
            },
        }

    def action_fetch_courier_services(self):
        return self.action_fetch_couriers_from_mintsoft()

    def action_validate_shipping_service(self):
        """Verify that mintsoft_service_id exists in GET /api/Courier/Services/{id}."""
        self.ensure_one()
        client = MintsoftClient(self.instance_id)
        try:
            srv = client.request(
                'GET',
                f'/api/Courier/Services/{self.mintsoft_service_id}',
                operation='validate_courier_service',
                allow_404=True,
            )
        except MintsoftAPIError as err:
            raise UserError(_("Courier service validation failed: %s") % err.message) from err

        if not isinstance(srv, dict) or not srv.get('ID'):
            raise UserError(
                _("Mintsoft Courier Service ID %s was not found in Mintsoft.") % self.mintsoft_service_id
            )

        self.write({
            'mintsoft_service_name': srv.get('Name') or self.mintsoft_service_name,
            'mintsoft_courier_type_id': int(srv.get('CourierServiceTypeId') or self.mintsoft_courier_type_id or 0),
            'tracking_url_template': srv.get('TrackingURL') or self.tracking_url_template or '',
            'last_verified_at': fields.Datetime.now(),
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Shipping Service Validated'),
                'message': _('Verified Mintsoft Service "%s" (ID %s).') % (self.mintsoft_service_name, self.mintsoft_service_id),
                'type': 'success',
            },
        }

    def action_test_mapping(self):
        return self.action_validate_shipping_service()
