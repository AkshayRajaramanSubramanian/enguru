const express = require('express');
const db = require('../db');
const { requireUser } = require('../auth');

const router = express.Router();
router.use(requireUser);

const PAGE_SIZE = 20;

function loadOwnNote(req, res) {
  const note = db.prepare('SELECT * FROM notes WHERE id = ?').get(req.params.id);
  if (!note || note.owner_id !== req.user.id) {
    res.status(404).json({ error: 'not_found' });
    return null;
  }
  return note;
}

router.get('/', (req, res) => {
  const page = Math.max(parseInt(req.query.page, 10) || 1, 1);
  const offset = (page - 1) * PAGE_SIZE;
  const notes = db.prepare(
    'SELECT id, title, updated_at FROM notes WHERE owner_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?'
  ).all(req.user.id, PAGE_SIZE, offset);
  res.json({ page, notes });
});

router.get('/:id', (req, res) => {
  const note = loadOwnNote(req, res);
  if (note) res.json(note);
});

router.post('/', (req, res) => {
  const { title, body = '' } = req.body;
  if (!title) return res.status(400).json({ error: 'title_required' });
  const info = db.prepare(
    'INSERT INTO notes (owner_id, title, body, updated_at) VALUES (?, ?, ?, ?)'
  ).run(req.user.id, title, body, Date.now());
  res.status(201).json({ id: info.lastInsertRowid });
});

// Save edits to a note.
router.put('/:id', (req, res) => {
  const { title, body } = req.body;
  const info = db.prepare(
    'UPDATE notes SET title = COALESCE(?, title), body = COALESCE(?, body), updated_at = ? WHERE id = ?'
  ).run(title, body, Date.now(), req.params.id);
  if (info.changes === 0) return res.status(404).json({ error: 'not_found' });
  res.json({ ok: true });
});

router.delete('/:id', (req, res) => {
  const note = loadOwnNote(req, res);
  if (!note) return;
  db.prepare('DELETE FROM notes WHERE id = ?').run(note.id);
  res.status(204).end();
});

module.exports = router;
