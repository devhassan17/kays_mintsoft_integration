from odoo import api, fields, models, _


class ProductProduct(models.Model):
    _inherit = 'product.product'

    kays_mapping_ids = fields.One2many(
        'kays.mintsoft.product.mapping',
        'product_id',
        string='Kays SKU Mappings',
    )
    kays_mapping_count = fields.Integer(
        compute='_compute_kays_mapping_info',
        string='Kays Mappings',
    )
    kays_mintsoft_product_id = fields.Integer(
        compute='_compute_kays_mapping_info',
        string='Mintsoft Product ID',
    )
    kays_mintsoft_stock = fields.Float(
        compute='_compute_kays_mapping_info',
        string='Mintsoft Available Stock',
    )

    @api.depends('kays_mapping_ids', 'kays_mapping_ids.mintsoft_product_id', 'kays_mapping_ids.last_mintsoft_stock')
    def _compute_kays_mapping_info(self):
        for prod in self:
            mappings = prod.kays_mapping_ids.filtered(lambda m: m.company_id in self.env.companies)
            prod.kays_mapping_count = len(mappings)
            primary = mappings[:1]
            prod.kays_mintsoft_product_id = primary.mintsoft_product_id if primary else 0
            prod.kays_mintsoft_stock = sum(mappings.mapped('last_mintsoft_stock'))

    def action_open_kays_mappings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays SKU Mappings — %s') % self.display_name,
            'res_model': 'kays.mintsoft.product.mapping',
            'view_mode': 'list,form',
            'domain': [('product_id', '=', self.id)],
            'context': {
                'default_product_id': self.id,
                'default_odoo_sku': self.default_code or '',
                'default_mintsoft_sku': self.default_code or '',
            },
        }


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def action_open_kays_mappings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays SKU Mappings — %s') % self.display_name,
            'res_model': 'kays.mintsoft.product.mapping',
            'view_mode': 'list,form',
            'domain': [('product_id.product_tmpl_id', '=', self.id)],
        }
