class CouponError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

// Applies a coupon code inside the caller's checkout transaction.
// Returns the coupon row; throws CouponError when it cannot be used.
async function redeem(client, code) {
  const { rows } = await client.query(
    'SELECT id, percent_off, max_uses, times_used, expires_at FROM coupons WHERE code = $1',
    [code.trim().toUpperCase()]
  );
  const coupon = rows[0];
  if (!coupon || (coupon.expires_at && coupon.expires_at < new Date())) {
    throw new CouponError('invalid_coupon');
  }
  if (coupon.times_used >= coupon.max_uses) {
    throw new CouponError('coupon_exhausted');
  }
  await client.query('UPDATE coupons SET times_used = $1 WHERE id = $2', [
    coupon.times_used + 1,
    coupon.id,
  ]);
  return coupon;
}

function applyDiscount(totalCents, coupon) {
  return Math.round((totalCents * (100 - coupon.percent_off)) / 100);
}

module.exports = { redeem, applyDiscount, CouponError };
