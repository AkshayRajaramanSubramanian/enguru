class InventoryError extends Error {
  constructor(code, productId) {
    super(`${code}: ${productId}`);
    this.code = code;
    this.productId = productId;
  }
}

// Takes `quantity` units of a product out of stock, inside the caller's transaction.
// Returns the remaining stock; throws InventoryError if there is not enough.
async function reserve(client, productId, quantity) {
  const { rows } = await client.query(
    `UPDATE products
        SET stock = stock - $1
      WHERE id = $2 AND stock >= $1
      RETURNING stock`,
    [quantity, productId]
  );
  if (!rows[0]) {
    throw new InventoryError('out_of_stock', productId);
  }
  return rows[0].stock;
}

module.exports = { reserve, InventoryError };
