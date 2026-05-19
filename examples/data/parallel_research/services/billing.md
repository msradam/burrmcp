# Billing service

The billing service integrates with Stripe for payment processing.
Invoices are generated on the first of each month for all active
subscriptions.

## Charge retry policy

Failed charges retry up to four times over seven days: at 24h, 48h, 96h,
and 168h after the initial failure. After the fourth failure the
subscription is paused and a notification email goes out.

## Refunds

Refunds are processed via the admin panel by anyone with the
`billing.refund` role. Stripe handles the actual refund within 5-10
business days.

## Webhooks

Billing listens for Stripe webhooks at `/billing/stripe-webhook`. The
webhook signing secret is in the `stripe-webhook-secret` Vault entry.

## Reconciliation

A nightly job reconciles Acme's internal invoice records against
Stripe's. Discrepancies page billing-oncall.
