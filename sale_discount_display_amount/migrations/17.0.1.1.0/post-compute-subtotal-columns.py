import logging

from odoo import SUPERUSER_ID
from odoo.api import Environment

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("Compute discount columns")
    env = Environment(cr, SUPERUSER_ID, {})

    # Update records with no discount in batches
    _logger.info("Updating records with no discount...")
    cr.execute("""
        UPDATE sale_order_line
        SET price_subtotal_no_discount = price_subtotal
        WHERE id IN (
            SELECT id FROM sale_order_line 
            WHERE discount = 0.0
            LIMIT 1000
        )
    """)
    while cr.rowcount > 0:
        cr.execute("""
            UPDATE sale_order_line
            SET price_subtotal_no_discount = price_subtotal
            WHERE id IN (
                SELECT id FROM sale_order_line 
                WHERE discount = 0.0
                AND price_subtotal_no_discount IS NULL
                LIMIT 1000
            )
        """)

    # Update sale orders in batches
    _logger.info("Updating sale orders...")
    cr.execute("""
        UPDATE sale_order
        SET price_subtotal_no_discount = amount_untaxed
        WHERE id IN (
            SELECT id FROM sale_order
            WHERE price_subtotal_no_discount IS NULL
            LIMIT 1000
        )
    """)
    while cr.rowcount > 0:
        cr.execute("""
            UPDATE sale_order
            SET price_subtotal_no_discount = amount_untaxed
            WHERE id IN (
                SELECT id FROM sale_order
                WHERE price_subtotal_no_discount IS NULL
                LIMIT 1000
            )
        """)

    # Process orders with discounts in batches
    _logger.info("Processing orders with discounts...")
    batch_size = 100
    cr.execute("SELECT DISTINCT order_id FROM sale_order_line WHERE discount > 0.0")
    all_order_ids = [r[0] for r in cr.fetchall()]

    for i in range(0, len(all_order_ids), batch_size):
        batch_ids = all_order_ids[i:i + batch_size]
        _logger.info(f"Processing batch {i//batch_size + 1}, orders {i} to {i + len(batch_ids)}")
        
        orders = env["sale.order"].browse(batch_ids)
        for order in orders:
            order.order_line._update_discount_display_fields()
            # Commit transaction after each batch to free memory
            cr.commit()
