--liquibase formatted sql

--changeset atropos:001-create-purchases
CREATE TABLE purchases (
    purchase_id UUID PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    customer_id UUID NOT NULL,
    purchased_at TIMESTAMPTZ NOT NULL,
    currency CHAR(3) NOT NULL,
    total_amount NUMERIC(14, 2) NOT NULL CHECK (total_amount >= 0),
    payload JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE purchase_items (
    purchase_id UUID NOT NULL REFERENCES purchases (purchase_id) ON DELETE CASCADE,
    line_number SMALLINT NOT NULL CHECK (line_number BETWEEN 1 AND 10),
    product_id UUID NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0),
    line_total NUMERIC(14, 2) NOT NULL CHECK (line_total >= 0),
    PRIMARY KEY (purchase_id, line_number)
);

CREATE INDEX idx_purchases_purchased_at ON purchases (purchased_at DESC);
CREATE INDEX idx_purchases_customer_id ON purchases (customer_id);
CREATE INDEX idx_purchase_items_product_id ON purchase_items (product_id);

--rollback DROP TABLE IF EXISTS purchase_items;
--rollback DROP TABLE IF EXISTS purchases;
