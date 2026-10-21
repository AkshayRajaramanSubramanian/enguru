const express = require('express');
const { login } = require('./auth');
const notes = require('./routes/notes');

const app = express();
app.use(express.json({ limit: '100kb' }));

app.post('/login', (req, res) => {
  const token = login(String(req.body.username || ''), String(req.body.password || ''));
  if (!token) return res.status(401).json({ error: 'bad_credentials' });
  res.json({ token });
});

app.use('/notes', notes);

app.use((err, req, res, next) => {
  console.error(err);
  res.status(500).json({ error: 'internal' });
});

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`teamnotes listening on ${port}`));
