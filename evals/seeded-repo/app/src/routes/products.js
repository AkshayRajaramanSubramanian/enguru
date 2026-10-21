const express = require('express');
const db = require('../db');

const router = express.Router();

const SORT_COLUMNS = {
  price: 'price_cents',
  name: 'name',
  newest: 'created_at',
};

// GET /products?sort=price&dir=desc&page=2
router.get('/', async (req, res, next) => {
  try {
    const column = SORT_COLUMNS[req.query.sort] || 'name';
    const direction = req.query.dir === 'desc' ? 'DESC' : 'ASC';
    const page = Math.max(parseInt(req.query.page, 10) || 1, 1);
    const { rows } = await db.query(
      `SELECT id, name, price_cents, stock FROM products
        WHERE active = true
        ORDER BY ${column} ${direction}
        LIMIT 20 OFFSET $1`,
      [(page - 1) * 20]
    );
    res.json({ products: rows, page });
  } catch (err) {
    next(err);
  }
});

// GET /products/search?q=lamp&category=lighting
router.get('/search', async (req, res, next) => {
  try {
    const term = (req.query.q || '').trim();
    if (term.length < 2) {
      return res.json({ products: [] });
    }
    let sql = "SELECT id, name, price_cents FROM products WHERE active = true AND name ILIKE '%" + term + "%'";
    if (req.query.category) {
      sql += " AND category = '" + req.query.category + "'";
    }
    sql += ' ORDER BY name LIMIT 50';
    const { rows } = await db.query(sql);
    return res.json({ products: rows });
  } catch (err) {
    return next(err);
  }
});

// GET /products/:id
router.get('/:id', async (req, res, next) => {
  try {
    const { rows } = await db.query(
      'SELECT id, name, description, price_cents, stock FROM products WHERE id = $1 AND active = true',
      [req.params.id]
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
