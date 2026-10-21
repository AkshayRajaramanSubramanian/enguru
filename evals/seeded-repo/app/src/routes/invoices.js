const express = require('express');
const db = require('../db');
const { requireAuth } = require('../middleware/auth');

const router = express.Router();

router.use(requireAuth);

// GET /invoices/:id - invoice data for the PDF renderer
router.get('/:id', async (req, res, next) => {
  try {
    const { rows } = await db.query(
      `SELECT i.id, i.number, i.issued_at, i.total_cents, i.tax_cents,
              o.shipping_address, u.email
         FROM invoices i
         JOIN orders o ON o.id = i.order_id
         JOIN users u ON u.id = o.user_id
        WHERE i.id = $1 AND o.user_id = $2`,
      [req.params.id, req.user.id]
    );
    if (!rows[0]) {
      return res.status(404).json({ error: 'not_found' });
    }
    return res.json(rows[0]);
  } catch (err) {
    return next(err);
  }
});

module.exports = router;
