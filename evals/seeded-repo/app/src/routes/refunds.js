const express = require('express');
const db = require('../db');
const payments = require('../services/payments');
const { requireAuth, requireRole } = require('../middleware/auth');

const router = express.Router();

router.use(requireAuth, requireRole('support'));

// POST /refunds { orderId, amountCents? } - support staff refund a paid order
router.post('/', async (req, res, next) => {
  try {
    const { orderId, amountCents } = req.body || {};
    const { rows } = await db.query(
      'SELECT id, status, total_cents, payment_intent_id FROM orders WHERE id = $1',
      [orderId]
    );
    const order = rows[0];
    if (!order) {
      return res.status(404).json({ error: 'not_found' });
    }
    if (order.status !== 'paid') {
      return res.status(409).json({ error: 'not_refundable', status: order.status });
    }
    const requested = parseInt(amountCents, 10);
    const amount = requested > 0 ? Math.min(requested, order.total_cents) : order.total_cents;
    let refund;
    try {
      refund = await payments.refund({ paymentIntentId: order.payment_intent_id, amountCents: amount });
    } catch (err) {
      console.error('refund failed for order', order.id, err.message);
      return res.status(502).json({ error: 'refund_failed' });
    }
    await db.query(
      "UPDATE orders SET status = $2, refund_id = $3, refunded_cents = $4 WHERE id = $1",
      [order.id, amount === order.total_cents ? 'refunded' : 'partially_refunded', refund.id, amount]
    );
    return res.json({ ok: true, refundId: refund.id, amountCents: amount });
  } catch (err) {
    return next(err);
  }
});

module.exports = router;
