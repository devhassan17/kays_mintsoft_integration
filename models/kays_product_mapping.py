from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.product_service import ProductService


class KaysMintsoftProductMapping(models.Model):
    _name = 'kays.mintsoft.product.mapping'
    _description = 'Kays Logistics Product & SKU Mapping'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'mintsoft_sku, id'
    _check_company_auto = True

    name = fields.Char(
        string='Mapping Name',
        compute='_compute_name',
        store=True,
    )
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
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
    product_id = fields.Many2one(
        'product.product',
        string='Odoo Product',
        index=True,
        tracking=True,
    )
    odoo_sku = fields.Char(string='Odoo SKU', index=True, tracking=True)
    odoo_barcode = fields.Char(string='Odoo Barcode')
    uom_id = fields.Many2one(
        'uom.uom',
        related='product_id.uom_id',
        string='Odoo Base UoM',
        readonly=True,
    )
    mintsoft_uom_factor = fields.Float(
        string='Mintsoft Units per Odoo Base UoM',
        default=1.0,
        required=True,
        help='Multiplier to convert 1 Odoo Base UoM into Mintsoft stock units (e.g. 1.0 for single unit, 12.0 if Odoo product is a Case of 12 tracked as singles in Mintsoft).',
    )

    mintsoft_product_id = fields.Integer(string='Mintsoft Product ID', index=True, tracking=True)
    mintsoft_sku = fields.Char(string='Mintsoft SKU', required=True, index=True, tracking=True)
    mintsoft_name = fields.Char(string='Mintsoft Product Name')
    mintsoft_description = fields.Text(string='Mintsoft Description')
    mintsoft_barcode = fields.Char(string='Mintsoft Barcode (EAN/UPC)')
    mintsoft_weight = fields.Float(string='Weight (kg)')
    mintsoft_height = fields.Float(string='Height (cm)')
    mintsoft_width = fields.Float(string='Width (cm)')
    mintsoft_depth = fields.Float(string='Depth (cm)')
    mintsoft_volume = fields.Float(string='Volume')
    is_bundle = fields.Boolean(string='Is Mintsoft Bundle', default=False)
    discontinued_in_mintsoft = fields.Boolean(string='Discontinued in Mintsoft', default=False)

    mapping_status = fields.Selection(
        selection=[
            ('matched', 'Matched'),
            ('missing_in_odoo', 'Missing in Odoo'),
            ('missing_in_mintsoft', 'Missing in Mintsoft'),
            ('duplicate_sku', 'Duplicate SKU'),
            ('mismatch', 'Mismatch'),
            ('disabled', 'Disabled'),
        ],
        string='Mapping Status',
        default='missing_in_mintsoft',
        required=True,
        index=True,
        tracking=True,
    )
    sync_stock_enabled = fields.Boolean(string='Include in Stock Sync', default=True)
    last_mintsoft_stock = fields.Float(string='Mintsoft Available', readonly=True)
    last_odoo_stock = fields.Float(string='Odoo Available', readonly=True)
    last_sync_at = fields.Datetime(string='Last Sync', readonly=True)
    last_error = fields.Text(string='Error', readonly=True)

    @api.depends('mintsoft_sku', 'product_id.display_name', 'mintsoft_product_id')
    def _compute_name(self):
        for rec in self:
            prod_label = rec.product_id.display_name if rec.product_id else _('Unlinked')
            rec.name = f"{rec.mintsoft_sku or 'NO-SKU'} <-> {prod_label}"

    @api.onchange('product_id')
    def _onchange_product_id(self):
        for rec in self:
            if rec.product_id:
                rec.odoo_sku = rec.product_id.default_code or ''
                rec.odoo_barcode = rec.product_id.barcode or ''
                if not rec.mintsoft_sku and rec.product_id.default_code:
                    rec.mintsoft_sku = rec.product_id.default_code

    # -------------------------------------------------------------------------
    # Product Mapping Action Buttons (Part G - Section 12)
    # -------------------------------------------------------------------------
    def action_validate_mapping(self):
        """Validate local and remote SKU consistency."""
        svc = ProductService(self.env)
        matched = 0
        for rec in self:
            if rec.product_id and not rec.odoo_sku:
                rec.odoo_sku = rec.product_id.default_code or ''
            if svc.refresh_single_mapping(rec):
                matched += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Mapping Validation Complete'),
                'message': _('Validated %d mapping(s); %d matched with Mintsoft.') % (len(self), matched),
                'type': 'success' if matched == len(self) else 'warning',
            },
        }

    def action_refresh_mintsoft_product(self):
        """Refresh product metadata from Mintsoft."""
        return self.action_validate_mapping()

    def action_create_mapping(self):
        """Link an unlinked mapping to an existing Odoo product by matching SKU or barcode."""
        for rec in self:
            if rec.product_id:
                continue
            Product = self.env['product.product'].with_company(rec.company_id)
            prods = Product.search([
                ('default_code', '=', rec.mintsoft_sku),
                ('company_id', 'in', [False, rec.company_id.id]),
            ])
            if len(prods) == 1:
                rec.write({
                    'product_id': prods[0].id,
                    'odoo_sku': prods[0].default_code,
                    'odoo_barcode': prods[0].barcode or '',
                    'mapping_status': 'matched',
                    'last_error': False,
                })
            elif len(prods) > 1:
                rec.write({
                    'mapping_status': 'duplicate_sku',
                    'last_error': _('Multiple Odoo products share SKU %s.') % rec.mintsoft_sku,
                })
        return True

    def action_import_product(self):
        """Create or link Odoo product from Mintsoft metadata."""
        svc = ProductService(self.env)
        for rec in self:
            svc.import_product_to_odoo(rec)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Product Imported'),
                'message': _('Imported/linked %d product(s) in Odoo.') % len(self),
                'type': 'success',
            },
        }

    def action_export_product(self):
        """Export Odoo product to Mintsoft (requires enable_product_export=True)."""
        svc = ProductService(self.env)
        for rec in self:
            svc.export_product_to_mintsoft(rec)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Product Exported'),
                'message': _('Exported %d product(s) to Mintsoft.') % len(self),
                'type': 'success',
            },
        }

    def action_view_errors(self):
        self.ensure_one()
        if not self.last_error:
            raise UserError(_("No errors recorded on this product mapping."))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Mapping Error — %s') % self.mintsoft_sku,
                'message': self.last_error,
                'type': 'danger',
                'sticky': True,
            },
        }
