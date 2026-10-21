const users = require('../services/users');

// Reads the bearer token, loads the user, and attaches it to req.user.
async function requireAuth(req, res, next) {
  const header = req.get('authorization') || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (!token) {
    return res.status(401).json({ error: 'unauthenticated' });
  }
  try {
    const user = await users.findBySessionToken(token);
    if (!user) {
      return res.status(401).json({ error: 'unauthenticated' });
    }
    await users.updateLastSeen(user.id);
    req.user = user;
    return next();
  } catch (err) {
    return next(err);
  }
}

function requireRole(role) {
  return (req, res, next) => {
    if (!req.user || req.user.role !== role) {
      return res.status(403).json({ error: 'forbidden' });
    }
    return next();
  };
}

module.exports = { requireAuth, requireRole };
