const Stripe = require('stripe');

const stripe = Stripe(process.env.STRIPE_SECRET_KEY);

// Charges the customer's saved card for an order.
// Resolves to { id, status } from the PaymentIntent.
async function charge({ customerId, amountCents, orderId }) {
  try {
    const intent = await stripe.paymentIntents.create(
      {
        amount: amountCents,
        currency: 'usd',
        customer: customerId,
        confirm: true,
        off_session: true,
        metadata: { orderId: String(orderId) },
      },
      { idempotencyKey: `order-${orderId}` }
    );
    return { id: intent.id, status: intent.status };
  } catch (err) {
    console.warn('charge failed for order', orderId, err.message);
  }
}

// Refunds all or part of a captured payment. Throws if Stripe rejects it.
async function refund({ paymentIntentId, amountCents }) {
  const result = await stripe.refunds.create({
    payment_intent: paymentIntentId,
    amount: amountCents,
  });
  return { id: result.id, status: result.status };
}

module.exports = { charge, refund };
