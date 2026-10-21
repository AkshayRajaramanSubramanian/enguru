const express = require('express');
const bcrypt = require('bcrypt');
const db = require('../db');
const users = require('../services/users');
const { requireAuth } = require('../middleware/auth');

const router = express.Router();

router.use(requireAuth);

// GET /account - the signed-in user's profile
router.get('/', (req, res) => {
  res.json({ id: req.user.id, email: req.user.email, role: req.user.role });
});

// POST /account/password { currentPassword, newPassword }
// Changes the password and signs out every other session.
router.post('/password', async (req, res, next) => {
  try {
    const { currentPassword, newPassword } = req.body || {};
    if (!newPassword || newPassword.length < 10) {
      return res.status(400).json({ error: 'password_too_short' });
    }
    const user = await users.findById(req.user.id);
    const matches = await bcrypt.compare(currentPassword || '', user.password_hash);
    if (!matches) {
      return res.status(403).json({ error: 'wrong_password' });
    }
    const hash = await bcrypt.hash(newPassword, 12);
    const currentToken = req.get('authorization').slice(7);
    db.query('UPDATE users SET password_hash = $1, password_changed_at = now() WHERE id = $2', [
      hash,
      user.id,
    ]);
    await db.query('DELETE FROM sessions WHERE user_id = $1 AND token <> $2', [user.id, currentToken]);
    return res.json({ ok: true });
  } catch (err) {
    return next(err);
  }
});

module.exports = router;
