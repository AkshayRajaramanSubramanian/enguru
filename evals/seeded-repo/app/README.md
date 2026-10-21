# shopfront-api

A small Express + Postgres API behind the Shopfront web app.

- `GET /products`, `GET /products/search?q=` - catalogue
- `GET /orders`, `GET /orders/:id` - a customer's orders
- `POST /checkout` - create an order, apply a coupon, charge the card
- `GET /invoices/:id` - PDF-ready invoice data
- `POST /account/password` - change password
- `POST /refunds` - refund an order (support staff)

Configuration comes from `DATABASE_URL` and `STRIPE_SECRET_KEY`.
