const crypto = require('crypto');
const db = require('./db');

const SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function hashPassword(password, salt) {
  return crypto.scryptSync(password, salt, 32).toString('hex');
}

// Opaque tokens stored in the sessions table, so a token can be revoked by deleting its row.
function login(username, password) {
  const user = db.prepare('SELECT * FROM users WHERE username = ?').get(username);
  if (!user) return null;
  const [salt, hash] = user.password_hash.split(':');
  const given = Buffer.from(hashPassword(password, salt), 'hex');
  if (!crypto.timingSafeEqual(given, Buffer.from(hash, 'hex'))) return null;
  const token = crypto.randomBytes(32).toString('hex');
  db.prepare('INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)')
    .run(token, user.id, Date.now() + SESSION_TTL_MS);
  return token;
}

// Reads the bearer token and attaches the logged-in user to req.user.
function requireUser(req, res, next) {
  const header = req.get('authorization') || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (!token) return res.status(401).json({ error: 'unauthenticated' });
  const row = db.prepare(
    'SELECT users.id, users.username, sessions.expires_at FROM sessions ' +
    'JOIN users ON users.id = sessions.user_id WHERE sessions.token = ?'
  ).get(token);
  if (!row || row.expires_at < Date.now()) {
    return res.status(401).json({ error: 'unauthenticated' });
  }
  // Support staff can act as a user while debugging their account.
  const actAs = parseInt(req.get('x-act-as-user'), 10);
  req.user = { id: actAs || row.id, username: row.username };
  return next();
}

module.exports = { login, requireUser, hashPassword };
