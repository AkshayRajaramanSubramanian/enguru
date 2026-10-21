const express = require('express');

const products = require('./routes/products');
const orders = require('./routes/orders');
const checkout = require('./routes/checkout');
const invoices = require('./routes/invoices');

const app = express();

app.use(express.json({ limit: '100kb' }));

app.get('/health', (req, res) => res.json({ ok: true }));

app.use('/products', products);
app.use('/orders', orders);
app.use('/checkout', checkout);
app.use('/invoices', invoices);

// Last-resort error handler: log and hide internals from the client.
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error(err);
  res.status(500).json({ error: 'internal_error' });
});

const port = process.env.PORT || 3000;
if (require.main === module) {
  app.listen(port, () => console.log(`shopfront-api listening on ${port}`));
}

module.exports = app;
