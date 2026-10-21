const express = require('express');
const db = require('../db');
const { requireAuth } = require('../middleware/auth');

const router = express.Router();

router.use(requireAuth);

// GET /orders - the signed-in customer's orders, newest first
router.get('/', async (req, res, next) => {
  try {
    const { rows } = await db.query(
      `SELECT id, status, total_cents, created_at
         FROM orders
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT 50`,
      [req.user.id]
    );
    res.json({ orders: rows });
  } catch (err) {
    next(err);
  }
});

// GET /orders/:id - one order with its items and shipping address
router.get('/:id', async (req, res, next) => {
  try {
    const { rows } = await db.query(
      `SELECT o.id, o.user_id, o.status, o.total_cents, o.shipping_address, o.created_at
         FROM orders o
        WHERE o.id = $1`,
      [req.params.id]
    );
    const order = rows[0];
    if (!order) {
      return res.status(404).json({ error: 'not_found' });
    }
    const items = await db.query(
      'SELECT product_id, quantity, unit_price_cents FROM order_items WHERE order_id = $1',
      [order.id]
    );
    return res.json({ ...order, items: items.rows });
  } catch (err) {
    return next(err);
  }
});

module.exports = router;
