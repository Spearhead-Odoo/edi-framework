# Copyright 2020 Creu Blanca
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import logging

from odoo import fields
from odoo.tests.common import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.component.core import Component
from odoo.addons.component.tests.common import TransactionComponentRegistryCase

_logger = logging.getLogger(__name__)


@tagged("-at_install", "post_install")
class EDIBackendTestCase(AccountTestInvoicingCommon, TransactionComponentRegistryCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_registry(cls)
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

        class AccountMoveEventListenerDemo(Component):
            _name = "account.move.event.listener.demo"
            _inherit = "base.event.listener"

            def on_post_account_move(self, move):
                move.name = "new_name"

            def on_paid_account_move(self, move):
                move.name = "paid"

            def on_cancel_account_move(self, move):
                move.name = "cancelled"

        AccountMoveEventListenerDemo._build_component(cls.comp_registry)
        cls.comp_registry._cache.clear()
        cls.test_move = (
            cls.env["account.move"]
            .with_context(components_registry=cls.comp_registry)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": cls.partner_a.id,
                    "date": fields.Date.from_string("2016-01-01"),
                    "invoice_line_ids": [
                        (
                            0,
                            None,
                            {
                                "name": "revenue line 1",
                                "account_id": cls.company_data[
                                    "default_account_revenue"
                                ].id,
                                "quantity": 1.0,
                                "price_unit": 100.0,
                            },
                        ),
                        (
                            0,
                            None,
                            {
                                "name": "revenue line 2",
                                "account_id": cls.company_data[
                                    "default_account_revenue"
                                ].id,
                                "quantity": 1.0,
                                "price_unit": 100.0,
                                "tax_ids": [
                                    (6, 0, cls.company_data["default_tax_sale"].ids)
                                ],
                            },
                        ),
                    ],
                }
            )
        )
        cls.test_move.invalidate_recordset()

    def test_paid_move(self):
        self.test_move.action_post()
        self.assertEqual(self.test_move.name, "new_name")

        payment_action = self.test_move.action_register_payment()
        payment = (
            self.env[payment_action["res_model"]]
            .with_context(**payment_action["context"])
            .create(
                {
                    "journal_id": self.company_data["default_journal_bank"].id,
                }
            )
        )
        payment.with_context(
            components_registry=self.comp_registry
        ).action_create_payments()
        self.assertEqual(self.test_move.name, "paid")

    def test_cancel_move(self):
        self.test_move.action_post()
        self.assertEqual(self.test_move.name, "new_name")
        self.test_move.button_cancel()
        self.assertEqual(self.test_move.name, "cancelled")
