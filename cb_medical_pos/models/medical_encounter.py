# Copyright 2017 Creu Blanca
# Copyright 2017 Eficent Business and IT Consulting Services, S.L.
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class MedicalEncounter(models.Model):
    _inherit = "medical.encounter"

    pos_session_id = fields.Many2one(
        comodel_name="pos.session",
        string="PoS Session",
        readonly=1,
        track_visibility=True,
    )
    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        readonly=1,
        track_visibility=True,
    )
    currency_id = fields.Many2one(
        "res.currency", related="company_id.currency_id", readonly=True
    )
    pending_private_amount = fields.Monetary(
        currency_field="currency_id", compute="_compute_pending_private_amount"
    )
    laboratory_request_ids = fields.One2many(
        "medical.laboratory.request", inverse_name="encounter_id"
    )

    @api.depends(
        "sale_order_ids.coverage_agreement_id",
        "sale_order_ids.amount_total",
        "sale_order_ids.bank_statement_line_ids",
        "sale_order_ids.invoice_ids.amount_total",
        "sale_order_ids.invoice_ids.bank_statement_line_ids",
    )
    def _compute_pending_private_amount(self):
        for record in self:
            inv = record.sale_order_ids.filtered(
                lambda r: not r.coverage_agreement_id and r.invoice_ids
            ).mapped("invoice_ids")
            orders = record.sale_order_ids.filtered(
                lambda r: not r.coverage_agreement_id and not r.invoice_ids
            )
            record.pending_private_amount = (
                sum(
                    inv.filtered(lambda r: r.type == "out_invoice").mapped(
                        "amount_total"
                    )
                )
                - sum(
                    inv.filtered(lambda r: r.type != "out_invoice").mapped(
                        "amount_total"
                    )
                )
                - sum(
                    inv.mapped("bank_statement_line_ids")
                    .filtered(lambda r: r.statement_id.pos_session_id)
                    .mapped("amount")
                )
                + sum(orders.mapped("amount_total"))
                - sum(
                    orders.mapped("bank_statement_line_ids")
                    .filtered(lambda r: r.statement_id.pos_session_id)
                    .mapped("amount")
                )
            )

    def _get_sale_order_vals(
        self, partner, cov, agreement, third_party_partner, is_insurance
    ):
        vals = super()._get_sale_order_vals(
            partner, cov, agreement, third_party_partner, is_insurance
        )
        session = self.pos_session_id.id or self._context.get("pos_session_id")
        if session:
            vals["pos_session_id"] = session
        if not is_insurance:
            if not self.company_id.id and not self._context.get("company_id"):
                raise ValidationError(
                    _("Company is required in order to create Sale Orders")
                )
            vals["company_id"] = self.company_id.id or self._context.get(
                "company_id"
            )
        partner_obj = (
            self.env["res.partner"]
            .browse(partner)
            .with_context(force_company=vals["company_id"])
        )
        if "payment_term_id" not in vals:
            term = partner_obj.property_payment_term_id
            if term:
                vals["payment_term_id"] = term.id
        if "fiscal_position_id" not in vals:
            position = partner_obj.property_account_position_id
            if position:
                vals["fiscal_position_id"] = position.id
        addr = partner_obj.address_get(["delivery", "invoice"])
        if "partner_invoice_id" not in vals:
            vals["partner_invoice_id"] = addr["invoice"]
        if "partner_shipping_id" not in vals:
            vals["partner_shipping_id"] = addr["delivery"]
        return vals

    def inprogress2onleave_values(self):
        res = super().inprogress2onleave_values()
        if not self.company_id:
            if not self._context.get("company_id", False):
                raise ValidationError(_("Company is required"))
            res["company_id"] = self._context.get("company_id", False)
        if self._context.get("pos_session_id", False):
            res["pos_session_id"] = self._context.get("pos_session_id", False)
        return res

    @api.multi
    def inprogress2onleave(self):
        if self.laboratory_request_ids.filtered(
            lambda r: not r.laboratory_event_ids and r.state != "cancelled"
        ):
            raise ValidationError(_("Laboratory requests are not fulfilled."))
        self.create_sale_order()
        res = super().inprogress2onleave()
        sos = self.sale_order_ids.filtered(
            lambda r: not r.coverage_agreement_id and not r.is_down_payment
        )
        if not sos or all(so.amount_total == 0 for so in sos):
            self.onleave2finished()
        return res

    def finish_sale_order(self, sale_order):
        if not self._context.get("pos_session_id", False):
            raise ValidationError(
                _(
                    "Payment journal is necessary in order to finish sale orders"
                )
            )
        sale_order.action_confirm()
        cash_vals = {}
        if not sale_order.third_party_order:
            model = "cash.invoice.out"
            patient_journal = sale_order.company_id.patient_journal_id.id
            invoice = self.env["account.invoice"].browse(
                sale_order.with_context(
                    default_journal_id=patient_journal,
                    no_check_lines=True,
                    no_split_invoices=True,
                ).action_invoice_create()
            )
            invoice.action_invoice_open()
            if invoice.amount_total == 0:
                return
            amount = invoice.amount_total
            if invoice.type == "out_refund":
                amount = -amount
            cash_vals.update({"invoice_id": invoice.id, "amount": amount})
        else:
            model = "cash.sale.order.out"
            cash_vals.update(
                {
                    "sale_order_id": sale_order.id,
                    "amount": sale_order.amount_total,
                }
            )
        if cash_vals["amount"] != 0:
            if not self._context.get("journal_id", False):
                raise ValidationError(
                    _(
                        "Payment journal is necessary in order to "
                        "finish sale orders"
                    )
                )
            journal_id = self._context.get("journal_id", False)
            pos_session_id = self._context.get("pos_session_id", False)
            cash_vals["journal_id"] = journal_id
            process = (
                self.env[model]
                .with_context(
                    active_ids=[pos_session_id], active_model="pos.session"
                )
                .create(cash_vals)
            )
            process.run()

    def onleave2finished_values(self):
        res = super().onleave2finished_values()
        if self._context.get("pos_session_id", False):
            res["pos_session_id"] = self._context.get("pos_session_id", False)
        return res

    @api.multi
    def onleave2finished(self):
        for res in self.filtered(lambda r: r.state == "onleave"):
            sale_orders = res.sale_order_ids.filtered(
                lambda r: not r.coverage_agreement_id and not r.is_down_payment
            )
            for sale_order in sale_orders:
                res.finish_sale_order(sale_order)
        return super().onleave2finished()

    def down_payment_inverse_vals(self, order, line):
        return {
            "order_id": order.id,
            "product_id": line.product_id.id,
            "name": "%s (%s)"
            % (line.invoice_lines[0].invoice_id.number, line.name),
            "product_uom_qty": line.product_uom_qty,
            "product_uom": line.product_uom.id,
            "price_unit": -line.price_unit,
            "down_payment_line_id": line.invoice_lines[0].id,
        }

    def get_sale_order_lines(self):
        values = super().get_sale_order_lines()
        down_payments = self.sale_order_ids.filtered(
            lambda r: r.is_down_payment
        )
        if down_payments:
            if 0 not in values:
                values[0] = {}
            if self.get_patient_partner() not in values[0]:
                values[0][self.get_patient_partner()] = {}
            if 0 not in values[0][self.get_patient_partner()]:
                values[0][self.get_patient_partner()][0] = {}
            if 0 not in values[0][self.get_patient_partner()][0]:
                values[0][self.get_patient_partner()][0][0] = []
        return values

    def _generate_sale_order(
        self, key, cov, partner, third_party_partner, order_lines
    ):
        order = super()._generate_sale_order(
            key, cov, partner, third_party_partner, order_lines
        )
        if key == 0 and not third_party_partner:
            orders = self.sale_order_ids.filtered(lambda r: r.is_down_payment)
            for pay in orders:
                for line in pay.order_line:
                    inverse_line = self.env["sale.order.line"].create(
                        self.down_payment_inverse_vals(order, line)
                    )
                    inverse_line.change_company_id()
        return order
