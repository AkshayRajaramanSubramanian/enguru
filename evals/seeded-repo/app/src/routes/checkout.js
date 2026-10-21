const express = require('express');
const db = require('../db');
const { requireAuth } = require('../middleware/auth');
const inventory = require('../services/inventory');
const coupons = require('../services/coupons');
const payments = require('../services/payments');

const router = express.Router();

class CheckoutError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

// Prices the cart, reserves stock, applies the coupon and creates a pending order,
// all in one transaction. Returns { id, totalCents }.
function createPendingOrder(userId, items, couponCode, shippingAddress) {
  return db.withTransaction(async (client) => {
    let totalCents = 0;
    const lines = [];
    for (const item of items) {
      const quantity = parseInt(item.quantity, 10);
      if (!(quantity > 0)) {
        throw new CheckoutError('bad_quantity');
      }
      const { rows } = await client.query(
        'SELECT id, price_cents FROM products WHERE id = $1 AND active = true',
        [item.productId]
      );
      if (!rows[0]) {
        throw new CheckoutError('unknown_product');
      }
      await inventory.reserve(client, rows[0].id, quantity);
      totalCents += rows[0].price_cents * quantity;
      lines.push({ productId: rows[0].id, quantity, unitPriceCents: rows[0].price_cents });
    }
    if (couponCode) {
      const coupon = await coupons.redeem(client, couponCode);
      totalCents = coupons.applyDiscount(totalCents, coupon);
    }
    const { rows } = await client.query(
      `INSERT INTO orders (user_id, status, total_cents, shipping_address)
       VALUES ($1, 'pending', $2, $3) RETURNING id`,
      [userId, totalCents, shippingAddress]
    );
    const orderId = rows[0].id;
    for (const line of lines) {
      await client.query(
        'INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents) VALUES ($1, $2, $3, $4)',
        [orderId, line.productId, line.quantity, line.unitPriceCents]
      );
    }
    return { id: orderId, totalCents };
  });
}

// POST /checkout { items: [{productId, quantity}], couponCode?, shippingAddress }
router.post('/', requireAuth, async (req, res, next) => {
  const { items, couponCode, shippingAddress } = req.body || {};
  if (!Array.isArray(items) || items.length === 0 || !shippingAddress) {
    return res.status(400).json({ error: 'bad_request' });
  }
  try {
    const order = await createPendingOrder(req.user.id, items, couponCode, shippingAddress);
    const payment = await payments.charge({
      customerId: req.user.stripe_customer_id,
      amountCents: order.totalCents,
      orderId: order.id,
    });
    if (payment && payment.status !== 'succeeded') {
      await db.query("UPDATE orders SET status = 'payment_failed' WHERE id = $1", [order.id]);
      return res.status(402).json({ error: 'payment_failed', orderId: order.id });
    }
    await db.query(
      "UPDATE orders SET status = 'paid', payment_intent_id = $2, paid_at = now() WHERE id = $1",
      [order.id, payment && payment.id]
    );
    return res.status(201).json({ orderId: order.id, totalCents: order.totalCents });
  } catch (err) {
    if (err instanceof CheckoutError || err instanceof coupons.CouponError) {
      return res.status(400).json({ error: err.code });
    }
    if (err instanceof inventory.InventoryError) {
      return res.status(409).json({ error: err.code, productId: err.productId });
    }
    return next(err);
  }
});

module.exports = router;
