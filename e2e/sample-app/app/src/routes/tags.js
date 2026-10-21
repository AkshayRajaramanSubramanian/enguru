const express = require('express');
const db = require('../db');
const { requireUser } = require('../auth');

const router = express.Router();
router.use(requireUser);

const TAG_RE = /^[a-z0-9-]{1,32}$/;

router.post('/notes/:id/tags', (req, res) => {
  const name = String(req.body.name || '').toLowerCase();
  if (!TAG_RE.test(name)) return res.status(400).json({ error: 'bad_tag' });
  const note = db.prepare('SELECT owner_id FROM notes WHERE id = ?').get(req.params.id);
  if (!note || note.owner_id !== req.user.id) {
    return res.status(404).json({ error: 'not_found' });
  }
  db.prepare('INSERT OR IGNORE INTO tags (note_id, owner_id, name) VALUES (?, ?, ?)')
    .run(req.params.id, req.user.id, name);
  res.status(201).json({ ok: true });
});

router.get('/tags', (req, res) => {
  const rows = db.prepare(
    'SELECT name, COUNT(*) AS count FROM tags WHERE owner_id = ? GROUP BY name ORDER BY name'
  ).all(req.user.id);
  res.json({ tags: rows });
});

module.exports = router;
