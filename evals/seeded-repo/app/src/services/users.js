const db = require('../db');

async function findBySessionToken(token) {
  const { rows } = await db.query(
    `SELECT u.id, u.email, u.role, u.stripe_customer_id
       FROM sessions s
       JOIN users u ON u.id = s.user_id
      WHERE s.token = $1 AND s.expires_at > now()`,
    [token]
  );
  return rows[0] || null;
}

function updateLastSeen(userId) {
  return db.query('UPDATE users SET last_seen_at = now() WHERE id = $1', [userId]);
}

async function findById(id) {
  const { rows } = await db.query('SELECT id, email, role, password_hash FROM users WHERE id = $1', [id]);
  return rows[0] || null;
}

module.exports = { findBySessionToken, updateLastSeen, findById };
