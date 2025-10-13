import logging

from odoo import SUPERUSER_ID
from odoo.api import Environment

_logger = logging.getLogger(__name__)

# Configuration: Number of most recent orders to process during migration
# Older orders will be computed on-demand when accessed
RECENT_ORDERS_LIMIT = 10000


def migrate(cr, version):
    """
    Optimized migration that only processes recent orders.
    
    Strategy:
    - Process only the most recent N orders (configurable via RECENT_ORDERS_LIMIT)
    - Older orders remain NULL and will be computed when needed via _compute_amount
    - Saves hours of migration time for historical data that may never be accessed
    
    To compute old orders later, use a Server Action with this code:
    
        # Compute discount fields for orders in a specific date range
        orders = env['sale.order'].search([
            ('date_order', '>=', '2020-01-01'),
            ('date_order', '<=', '2022-12-31'),
            ('price_subtotal_no_discount', '=', False)
        ], limit=1000)
        
        for order in orders:
            order.order_line._update_discount_display_fields()
    """
    env = Environment(cr, SUPERUSER_ID, {})
    
    # Get statistics
    cr.execute("SELECT COUNT(*) FROM sale_order")
    total_orders = cr.fetchone()[0]
    cr.execute("SELECT COUNT(*) FROM sale_order_line")
    total_lines = cr.fetchone()[0]
    
    _logger.info(f"Total orders in database: {total_orders:,}")
    _logger.info(f"Total lines in database: {total_lines:,}")
    _logger.info(f"Strategy: Processing only the most recent {RECENT_ORDERS_LIMIT:,} orders")
    
    # Get IDs of recent orders (by date_order or write_date)
    cr.execute("""
        SELECT id 
        FROM sale_order 
        ORDER BY COALESCE(date_order, write_date, create_date) DESC 
        LIMIT %(limit)s
    """, {'limit': RECENT_ORDERS_LIMIT})
    recent_order_ids = [row[0] for row in cr.fetchall()]
    
    if not recent_order_ids:
        _logger.warning("No orders found to process")
        return
    
    _logger.info(f"Found {len(recent_order_ids)} recent orders to process")
    
    # Get count of lines for these orders
    cr.execute("""
        SELECT COUNT(*) 
        FROM sale_order_line 
        WHERE order_id = ANY(%(order_ids)s)
    """, {'order_ids': recent_order_ids})
    lines_to_process = cr.fetchone()[0]
    _logger.info(f"These orders contain {lines_to_process:,} lines")
    
    # ========== PHASE 1: Process lines without discount (SQL - Fast) ==========
    _logger.info("PHASE 1: Processing lines WITHOUT discount (SQL fast path)...")
    
    cr.execute("""
        SELECT COUNT(*) 
        FROM sale_order_line 
        WHERE order_id = ANY(%(order_ids)s)
          AND (discount = 0.0 OR discount IS NULL)
    """, {'order_ids': recent_order_ids})
    lines_no_discount = cr.fetchone()[0]
    _logger.info(f"  Lines without discount: {lines_no_discount:,}")
    
    batch_size = 50000
    processed = 0
    
    while True:
        cr.execute("""
            UPDATE sale_order_line sol
            SET 
                price_subtotal_no_discount = sol.price_subtotal,
                price_total_no_discount = sol.price_total,
                discount_subtotal = 0,
                discount_total = 0
            WHERE sol.id IN (
                SELECT id FROM sale_order_line 
                WHERE order_id = ANY(%(order_ids)s)
                  AND (discount = 0.0 OR discount IS NULL)
                  AND price_subtotal_no_discount IS NULL
                LIMIT %(batch_size)s
            )
        """, {'order_ids': recent_order_ids, 'batch_size': batch_size})
        
        rows_updated = cr.rowcount
        if rows_updated == 0:
            break
            
        processed += rows_updated
        _logger.info(f"    Processed {processed:,}/{lines_no_discount:,} lines ({processed*100//lines_no_discount if lines_no_discount > 0 else 0}%)")
        cr.commit()
    
    # ========== PHASE 2: Process lines with discount (ORM - Precise) ==========
    _logger.info("PHASE 2: Processing lines WITH discount (ORM precise path)...")
    
    cr.execute("""
        SELECT id 
        FROM sale_order_line 
        WHERE order_id = ANY(%(order_ids)s)
          AND discount > 0.0
          AND price_subtotal_no_discount IS NULL
        ORDER BY id
    """, {'order_ids': recent_order_ids})
    line_ids_with_discount = [row[0] for row in cr.fetchall()]
    total_discount_lines = len(line_ids_with_discount)
    _logger.info(f"  Lines with discount: {total_discount_lines:,}")
    
    if total_discount_lines > 0:
        batch_size_orm = 5000
        processed = 0
        
        for i in range(0, total_discount_lines, batch_size_orm):
            batch_ids = line_ids_with_discount[i:i + batch_size_orm]
            
            with env.cr.savepoint():
                lines = env["sale.order.line"].browse(batch_ids)
                for line in lines:
                    line._update_discount_display_fields()
            
            processed += len(batch_ids)
            _logger.info(f"    Processed {processed:,}/{total_discount_lines:,} lines ({processed*100//total_discount_lines}%)")
            cr.commit()
            env.clear()
    
    # ========== PHASE 3: Aggregate to sale_order ==========
    _logger.info("PHASE 3: Aggregating values to sale_order...")
    
    batch_size_orders = 5000
    processed = 0
    
    for i in range(0, len(recent_order_ids), batch_size_orders):
        batch_order_ids = recent_order_ids[i:i + batch_size_orders]
        
        cr.execute("""
            UPDATE sale_order so
            SET 
                price_subtotal_no_discount = subquery.sum_price_subtotal_no_discount,
                price_total_no_discount = subquery.sum_price_total_no_discount,
                discount_subtotal = subquery.sum_discount_subtotal,
                discount_total = subquery.sum_discount_total
            FROM (
                SELECT 
                    sol.order_id,
                    COALESCE(SUM(sol.price_subtotal_no_discount), 0) as sum_price_subtotal_no_discount,
                    COALESCE(SUM(sol.price_total_no_discount), 0) as sum_price_total_no_discount,
                    COALESCE(SUM(sol.discount_subtotal), 0) as sum_discount_subtotal,
                    COALESCE(SUM(sol.discount_total), 0) as sum_discount_total
                FROM sale_order_line sol
                WHERE sol.order_id = ANY(%(order_ids)s)
                GROUP BY sol.order_id
            ) subquery
            WHERE so.id = subquery.order_id
        """, {'order_ids': batch_order_ids})
        
        processed += len(batch_order_ids)
        _logger.info(f"  Processed {processed:,}/{len(recent_order_ids):,} orders ({processed*100//len(recent_order_ids)}%)")
        cr.commit()
    
    _logger.info(f"Migration completed successfully!")
    _logger.info(f"Processed {len(recent_order_ids):,} recent orders out of {total_orders:,} total")
    _logger.info(f"Older orders will be computed on-demand when accessed")
    _logger.info(f"To compute old orders in bulk, create a Server Action with order.order_line._update_discount_display_fields()")
